import torch
import torch.nn.functional as F

from typing import List, Optional

from src.utils.generation_algorithms import DecodingAlgorithm, GreedyAlgorithm, TopKAlgorithm, NucleusAlgorithm
from src.utils.generation_constraints import ABCBloomConstraint, MaxNewTokensConstraint, EosConstraint


class RemoteGenerationMixin:
    """
    A class containing all functions for auto-regressive text generation, to be used as a mixin in [`BloomForCausalLM`].
    The class exposes can be used for:
        - *greedy decoding*.
        - *multinomial sampling*.

    This class is similar to transformer's [`generation_utils.GenerationMixin`], it can be used instead of it. However, it has some differences.
    """

    def generate(
        self,
        inputs: Optional[torch.Tensor] = None,
        do_sample: Optional[bool] = None,
        temperature: float = 1.0,
        top_k: Optional[int] = None,
        top_p: Optional[float] = None,
        bos_token_id: Optional[int] = None,
        eos_token_id: Optional[int] = None,
        pad_token_id: Optional[int] = None,
        max_new_tokens: Optional[int] = None,
        decoding_algorithm: Optional[DecodingAlgorithm] = None,
        provided_constraints: List[ABCBloomConstraint] = [],
        **model_kwargs,
    ) -> torch.LongTensor:
        """
        Generates sequences of token ids for models with a language modeling head.

        :param inputs: The input tokens to the model.
        :param do_sample: Whether to sample from the model predictions or take the argmax.
        :param temperature: The temperature to use for sampling.
        :param top_k: The number of results to return.
        :param top_p: The cumulative probability of results to return.
        :param bos_token_id: The id of the beginning of sentence token.
        :param eos_token_id: The id of the end of sentence token.
        :param pad_token_id: The id of the padding token.
        :param max_new_tokens: The maximum number of tokens to generate.
        :param decoding_algorithm: The decoding algorithm to use.
        :param provided_constraints: A list of constraints to use.
        :param model_kwargs: Additional arguments to pass to the model.
        """

        assert (
            model_kwargs.get("logits_processor", None) is None
        ), "For RemoteGenerationMixin models use BloomConstraints instead of logits_processor"
        assert (
            model_kwargs.get("logits_wrapper", None) is None
        ), "For RemoveGenerationMixin models use DecodingAlgorithm instead of logits_wrapper"
        assert (
            model_kwargs.get("stopping_criteria", None) is None
        ), "For RemoteGenerationMixin models use BloomConstraints instead of stopping_criteria"

        bos_token_id = bos_token_id if bos_token_id is not None else self.config.bos_token_id
        pad_token_id = pad_token_id if pad_token_id is not None else self.config.pad_token_id
        eos_token_id = eos_token_id if eos_token_id is not None else self.config.eos_token_id

        if inputs is None:
            assert bos_token_id is not None, "You have to provide a bos_token_id if you do not provide inputs"
            inputs = torch.tensor([[bos_token_id]])

        if decoding_algorithm is None:
            if do_sample:
                decoding_algorithm = self._choose_sample_algorithm(temperature, top_k, top_p)
            else:
                decoding_algorithm = GreedyAlgorithm()

        constraints = self._get_constraints(
            inputs=inputs,
            eos_token_id=eos_token_id,
            pad_token_id=pad_token_id,
            max_new_tokens=max_new_tokens,
            provided_constraints=provided_constraints,
        )

        with self.transformer.h.inference_session() as sess:
            outputs = []
            if torch.any(inputs == pad_token_id):  # TODO: move to prepare_inputs
                outputs += [inputs[:, : inputs.size(1) - (inputs == pad_token_id).sum(-1).max()]]
            else:
                outputs += [inputs]
            last_token_id = None
            seq_idx = outputs[0].size(1)
            hypo_ids = torch.arange(outputs[0].size(0))
            while True:
                embs = self.transformer.word_embeddings(outputs[-1])
                embs = self.transformer.word_embeddings_layernorm(embs)
                hidden_state = sess.step(embs)[:, -1]
                hidden_state = self.transformer.ln_f(hidden_state)
                lm_logits = self.lm_head(hidden_state)

                for constraint in constraints:
                    lm_logits = constraint(last_token_id, lm_logits, hypo_ids)
                last_token_id, hypo_ids = decoding_algorithm(lm_logits)
                if seq_idx < inputs.size(1):  # TODO: why is it not a constraint?
                    pad_token_mask = inputs[:, seq_idx : seq_idx + 1] == pad_token_id
                    last_token_id = (~pad_token_mask) * inputs[
                        :, seq_idx : seq_idx + 1
                    ] + pad_token_mask * last_token_id

                if torch.all(last_token_id == eos_token_id):
                    break

                outputs.append(last_token_id)
                seq_idx += 1

        return torch.cat(outputs, dim=-1)

    def greedy_search(
        self,
        input_ids: torch.LongTensor,
        max_length: Optional[int] = None,
        pad_token_id: Optional[int] = None,
        eos_token_id: Optional[int] = None,
        provided_constraints: List[ABCBloomConstraint] = [],
        **model_kwargs,
    ) -> torch.LongTensor:
        """
        Generates sequences of token ids for models with a language modeling head. Uses greedy search.

        :param input_ids: The input tokens to the model.
        :param max_length: The maximum length of the sequence to generate.
        :param pad_token_id: The id of the padding token.
        :param eos_token_id: The id of the end of sentence token.
        :param provided_constraints: A list of constraints to use.
        """
        return self.generate(
            inputs=input_ids,
            max_new_tokens=max_length,
            pad_token_id=pad_token_id,
            eos_token_id=eos_token_id,
            decoding_algorithm=GreedyAlgorithm(),
            provided_constraints=provided_constraints,
            **model_kwargs,
        )

    def sample(
        self,
        input_ids: torch.LongTensor,
        temperature: float = 1.0,
        top_k: Optional[int] = None,
        top_p: Optional[float] = None,
        max_length: Optional[int] = None,
        pad_token_id: Optional[int] = None,
        eos_token_id: Optional[int] = None,
        provided_constraints: List[ABCBloomConstraint] = [],
        **model_kwargs,
    ) -> torch.LongTensor:
        """
        Generates sequences of token ids for models with a language modeling head. Uses sampling. Uses multinomial sampling algorithm. If top_k is provided, uses top_k sampling. If top_p is provided, uses nucleus sampling.

        :param: input_ids: The input tokens to the model.
        :param: temperature: The temperature to use for sampling.
        :param: top_k: The number of samples to use for top_k sampling.
        :param: top_p: The probability of using top_p sampling.
        :param: max_length: The maximum length of the sequence to generate.
        :param: pad_token_id: The id of the padding token.
        :param: eos_token_id: The id of the end of sentence token.
        :param: provided_constraints: A list of constraints to use.
        :param: model_kwargs: Additional kwargs to pass to the model.
        """

        return self.generate(
            inputs=input_ids,
            max_new_tokens=max_length,
            pad_token_id=pad_token_id,
            eos_token_id=eos_token_id,
            decoding_algorithm=self._choose_sample_algorithm(temperature, top_k, top_p),
            provided_constraints=provided_constraints,
            **model_kwargs,
        )

    def beam_search(
        self,
        input_ids: torch.LongTensor,
        max_length: Optional[int] = None,
        pad_token_id: Optional[int] = None,
        eos_token_id: Optional[int] = None,
        provided_constraints: List[ABCBloomConstraint] = [],
        **model_kwargs,
    ) -> torch.LongTensor:
        raise NotImplementedError

    def beam_sample(
        self,
        input_ids: torch.LongTensor,
        max_length: Optional[int] = None,
        pad_token_id: Optional[int] = None,
        eos_token_id: Optional[int] = None,
        provided_constraints: List[ABCBloomConstraint] = [],
        **model_kwargs,
    ) -> torch.LongTensor:
        raise NotImplementedError

    def group_beam_search(
        self,
        input_ids: torch.LongTensor,
        max_length: Optional[int] = None,
        pad_token_id: Optional[int] = None,
        eos_token_id: Optional[int] = None,
        provided_constraints: List[ABCBloomConstraint] = [],
        **model_kwargs,
    ) -> torch.LongTensor:
        raise NotImplementedError

    def _choose_sample_algorithm(
        self,
        temperature: float = 1.0,
        top_k: Optional[int] = None,
        top_p: Optional[float] = None,
    ) -> DecodingAlgorithm:
        if (top_k is not None) and (top_p is not None):
            raise ValueError("You have to provide only top_k or top_p for sampling")
        if top_k:
            return TopKAlgorithm(top_k, temperature)
        elif top_p:
            return NucleusAlgorithm(top_p, temperature)

    def _get_constraints(
        self,
        inputs: Optional[torch.Tensor] = None,
        eos_token_id: Optional[int] = None,
        pad_token_id: Optional[int] = None,
        max_new_tokens: Optional[int] = None,
        provided_constraints: List[ABCBloomConstraint] = [],
    ) -> List[ABCBloomConstraint]:
        constraints = []
        constraints.extend(provided_constraints)
        if max_new_tokens is not None:
            constraints.append(MaxNewTokensConstraint(inputs, max_new_tokens, eos_token_id, pad_token_id))
        constraints.append(EosConstraint(inputs, eos_token_id, pad_token_id))
        return constraints
