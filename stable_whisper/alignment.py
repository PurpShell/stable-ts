import copy
import re
import warnings

import torch
import numpy as np
from tqdm import tqdm
from typing import TYPE_CHECKING, Union, List, Callable, Optional, Tuple

import whisper
from whisper.tokenizer import get_tokenizer
from whisper.audio import (
    SAMPLE_RATE, N_FRAMES, N_SAMPLES, N_FFT, pad_or_trim, log_mel_spectrogram, FRAMES_PER_SECOND, CHUNK_LENGTH
)

from .result import WhisperResult, Segment
from .timing import add_word_timestamps_stable, split_word_tokens
from .audio import prep_audio
from .utils import warn_compatibility_issues, safe_print, format_timestamp

if TYPE_CHECKING:
    from whisper.model import Whisper

__all__ = ['align', 'refine', 'locate']


def align(
        model: "Whisper",
        audio: Union[str, np.ndarray, torch.Tensor, bytes],
        text: Union[str, List[int], WhisperResult],
        language: str = None,
        *,
        verbose: Optional[bool] = False,
        regroup: bool = True,
        suppress_silence: bool = True,
        suppress_word_ts: bool = True,
        min_word_dur: bool = 0.1,
        q_levels: int = 20,
        k_size: int = 5,
        vad: bool = False,
        vad_threshold: float = 0.35,
        vad_onnx: bool = False,
        demucs: Union[bool, torch.nn.Module] = False,
        demucs_output: str = None,
        demucs_options: dict = None,
        only_voice_freq: bool = False,
        prepend_punctuations: str = "\"'“¿([{-",
        append_punctuations: str = "\"'.。,，!！?？:：”)]}、",
        progress_callback: Callable = None,
        ignore_compatibility: bool = False,
        remove_instant_words: bool = False,
        token_step: int = 100,
        original_split: bool = False,
        tokenizer: "Tokenizer" = None
) -> Union[WhisperResult, None]:
    """
    Align plain text or tokens with audio at word-level.

    Since this is significantly faster than transcribing, it is a more efficient method for testing various settings
    without re-transcribing. This is also useful for timing a more correct transcript than one that Whisper can produce.

    Parameters
    ----------
    model : "Whisper"
        The Whisper ASR model modified instance
    audio : str or np.ndarray or torch.Tensor or bytes
        Path/URL to the audio file, the audio waveform, or bytes of audio file.
        If audio is :class:`np.ndarray` or :class:`torch.Tensor`, the audio must be already at sampled to 16kHz.
    text : str or list of int or stable_whisper.result.WhisperResult
        String of plain-text, list of tokens, or instance of :class:`stable_whisper.result.WhisperResult`.
    language : str, default None, uses ``language`` in ``text`` if it is a :class:`stable_whisper.result.WhisperResult`
        Language of ``text``. Required if ``text`` does not contain ``language``.
    remove_instant_words : bool, default False
        Whether to truncate any words with zero duration.
    token_step : int, default 100
        Max number of tokens to align each pass. Use higher values to reduce chance of misalignment.
    original_split : bool, default False
        Whether to preserve the original segment groupings. Segments are spit by line break if ``text`` is plain-text.
    tokenizer : "Tokenizer", default None, meaning a new tokenizer is created according ``language`` and ``model``
        A tokenizer to used tokenizer text and detokenize tokens.
    verbose : bool or None, default False
        Whether to display the text being decoded to the console.
        Displays all the details if ``True``. Displays progressbar if ``False``. Display nothing if ``None``.
    regroup : bool or str, default True, meaning the default regroup algorithm
        String for customizing the regrouping algorithm. False disables regrouping.
        Ignored if ``word_timestamps = False``.
    suppress_silence : bool, default True
        Whether to enable timestamps adjustments base the detected silence.
    suppress_word_ts : bool, default True
        Whether to adjust word timestamps base the detected silence. Only enabled if ``suppress_silence = True``.
    q_levels : int, default 20
        Quantization levels for generating timestamp suppression mask; ignored if ``vad = true``.
        Acts as a threshold to marking sound as silent.
        Fewer levels will increase the threshold of volume at which to mark a sound as silent.
    k_size : int, default 5
        Kernel size for avg-pooling waveform to generate timestamp suppression mask; ignored if ``vad = true``.
        Recommend 5 or 3; higher sizes will reduce detection of silence.
    demucs : bool or torch.nn.Module, default False
        Whether to preprocess ``audio`` with Demucs to isolate vocals / remove noise. Set ``demucs`` to an instance of
        a Demucs model to avoid reloading the model for each run.
        Demucs must be installed to use. Official repo, https://github.com/facebookresearch/demucs.
    demucs_output : str, optional
        Path to save the vocals isolated by Demucs as WAV file. Ignored if ``demucs = False``.
        Demucs must be installed to use. Official repo, https://github.com/facebookresearch/demucs.
    demucs_options : dict, optional
        Options to use for :func:`stable_whisper.audio.demucs_audio`.
    vad : bool, default False
        Whether to use Silero VAD to generate timestamp suppression mask.
        Silero VAD requires PyTorch 1.12.0+. Official repo, https://github.com/snakers4/silero-vad.
    vad_threshold : float, default 0.35
        Threshold for detecting speech with Silero VAD. Low threshold reduces false positives for silence detection.
    vad_onnx : bool, default False
        Whether to use ONNX for Silero VAD.
    min_word_dur : float, default 0.1
        Only allow suppressing timestamps that result in word durations greater than this value.
    only_voice_freq : bool, default False
        Whether to only use sound between 200 - 5000 Hz, where majority of human speech are.
    prepend_punctuations : str, default '"'“¿([{-)'
        Punctuations to prepend to next word.
    append_punctuations : str, default '.。,，!！?？:：”)]}、)'
        Punctuations to append to previous word.
    progress_callback : Callable, optional
        A function that will be called when transcription progress is updated.
        The callback need two parameters.
        The first parameter is a float for seconds of the audio that has been transcribed.
        The second parameter is a float for total duration of audio in seconds.
    ignore_compatibility : bool, default False
        Whether to ignore warnings for compatibility issues with the detected Whisper version.

    Returns
    -------
    stable_whisper.result.WhisperResult or None
        All timestamps, words, probabilities, and other data from the alignment of ``audio``. Return None if alignment
        fails and ``remove_instant_words = True``.

    Notes
    -----
    If ``token_step`` is less than 1, ``token_step`` will be set to its maximum value, 442. This value is computed with
    ``whisper.model.Whisper.dims.n_text_ctx`` - 6.

    IF ``original_split = True`` and a line break is found in middle of a word in ``text``, the split will occur after
    that word.

    ``regroup`` is ignored if ``original_split = True``.

    Examples
    --------
    >>> import stable_whisper
    >>> model = stable_whisper.load_model('base')
    >>> result = model.align('helloworld.mp3', 'Hello, World!', 'English')
    >>> result.to_srt_vtt('helloword.srt')
    Saved 'helloworld.srt'
    """
    if demucs_options is None:
        demucs_options = {}
    if demucs_output:
        if 'save_path' not in demucs_options:
            demucs_options['save_path'] = demucs_output
        warnings.warn('``demucs_output`` is deprecated. Use ``demucs_options`` with ``save_path`` instead. '
                      'E.g. demucs_options=dict(save_path="demucs_output.mp3")',
                      DeprecationWarning, stacklevel=2)
    max_token_step = model.dims.n_text_ctx - 6
    if token_step < 1:
        token_step = max_token_step
    elif token_step > max_token_step:
        raise ValueError(f'The max value for [token_step] is {max_token_step} but got {token_step}.')

    warn_compatibility_issues(whisper, ignore_compatibility)
    split_indices_by_char = []
    if isinstance(text, WhisperResult):
        if language is None:
            language = text.language
        if original_split and len(text.segments) > 1 and text.has_words:
            split_indices_by_char = np.cumsum([sum(len(w.word) for w in seg.words) for seg in text.segments])
        text = text.all_tokens() if text.has_words and all(w.tokens for w in text.all_words()) else text.text
    elif isinstance(text, str):
        if original_split and '\n' in text:
            text_split = [line if line.startswith(' ') else ' '+line for line in text.splitlines()]
            split_indices_by_char = np.cumsum([len(seg) for seg in text_split])
            text = ''.join(re.sub(r'\s', ' ', seg) for seg in text_split)
        else:
            text = re.sub(r'\s', ' ', text)
            if not text.startswith(' '):
                text = ' ' + text
    if language is None:
        raise TypeError('expected argument for language')
    if tokenizer is None:
        tokenizer = get_tokenizer(model.is_multilingual, language=language, task='transcribe')
    tokens = tokenizer.encode(text) if isinstance(text, str) else text
    tokens = [t for t in tokens if t < tokenizer.eot]
    _, (words, word_tokens), _ = split_word_tokens([dict(tokens=tokens)], tokenizer)

    audio = prep_audio(
        audio,
        demucs=demucs,
        demucs_options=demucs_options,
        only_voice_freq=only_voice_freq,
        verbose=verbose
    )

    sample_padding = int(N_FFT // 2) + 1
    seek_sample = 0
    total_samples = audio.shape[-1]
    total_tokens = sum(len(wt) for wt in word_tokens)
    finished_tokens = 0

    def get_curr_words():
        nonlocal words, word_tokens
        curr_tk_count = 0
        w, wt = [], []
        for _ in range(len(words)):
            tk_count = len(word_tokens[0])
            if curr_tk_count + tk_count > token_step and w:
                break
            w.append(words.pop(0))
            wt.append(word_tokens.pop(0))
            curr_tk_count += tk_count
        return w, wt
    result = []

    with tqdm(total=total_tokens, unit='token', disable=verbose is not False, desc='Align') as tqdm_pbar:

        def update_pbar(finish: bool = False):
            nonlocal finished_tokens
            if finish:
                finished_tokens = tqdm_pbar.total
            tqdm_pbar.update(finished_tokens - tqdm_pbar.n)
            if progress_callback is not None:
                progress_callback(seek=finished_tokens, total=total_tokens)

        while words and seek_sample < total_samples:
            curr_words, curr_word_tokens = get_curr_words()

            seek_sample_end = seek_sample + N_SAMPLES
            audio_segment = audio[seek_sample:seek_sample_end]
            segment_samples = audio_segment.shape[-1]
            time_offset = seek_sample / SAMPLE_RATE

            mel_segment = log_mel_spectrogram(audio_segment, padding=sample_padding)
            mel_segment = pad_or_trim(mel_segment, N_FRAMES).to(device=model.device)

            segment = dict(
                seek=time_offset,
                tokens=(curr_words, curr_word_tokens)
            )

            add_word_timestamps_stable(
                segments=[segment],
                model=model,
                tokenizer=tokenizer,
                mel=mel_segment,
                num_samples=segment_samples,
                split_callback=(lambda x, _: x),
                prepend_punctuations=prepend_punctuations,
                append_punctuations=append_punctuations
            )

            break_next = False
            while segment['words']:
                word = segment['words'][-1]
                if break_next or word['end'] - word['start'] == 0:
                    words.insert(0, word['word'])
                    word_tokens.insert(0, word['tokens'])
                    del segment['words'][-1]
                    if break_next:
                        break
                elif words:
                    break_next = True
                else:
                    break

            finished_tokens += sum(len(w['tokens']) for w in segment['words'])
            if segment['words']:
                seek_sample = round(segment['words'][-1]['end'] * SAMPLE_RATE)
            else:
                seek_sample += audio_segment.shape[-1]

            update_pbar()

            result.extend(segment['words'])

            if verbose:
                line = '\n'.join(
                    f"[{format_timestamp(word['start'])}] -> "
                    f"[{format_timestamp(word['end'])}] \"{word['word']}\""
                    for word in segment.get('words', [])
                )
                safe_print(line)

        if not result:
            warnings.warn('Failed to align text.')

        if words and not remove_instant_words:
            total_duration = round(total_samples / SAMPLE_RATE, 3)
            result.extend(
                [
                    dict(word=w, start=total_duration, end=total_duration, probability=0.0, tokens=wt)
                    for w, wt in zip(words, word_tokens)
                ]
            )

        update_pbar(True)
    if not result:
        return

    if len(split_indices_by_char):
        word_lens = np.cumsum([[len(w['word']) for w in result]])
        split_indices = [(word_lens >= i).nonzero()[0][0]+1 for i in split_indices_by_char]
        result = WhisperResult([result[i:j] for i, j in zip([0]+split_indices[:-1], split_indices)])
    else:
        result = WhisperResult([result])

    if suppress_silence:
        result.adjust_by_silence(
            audio, vad,
            vad_onnx=vad_onnx, vad_threshold=vad_threshold,
            q_levels=q_levels, k_size=k_size,
            sample_rate=SAMPLE_RATE, min_word_dur=min_word_dur,
            word_level=suppress_word_ts, verbose=verbose
        )
    if not original_split:
        result.regroup(regroup)

    return result


def refine(
        model: "Whisper",
        audio: Union[str, np.ndarray, torch.Tensor, bytes],
        result: WhisperResult,
        *,
        steps: str = None,
        rel_prob_decrease: float = .03,
        abs_prob_decrease: float = .05,
        rel_rel_prob_decrease: Optional[float] = None,
        prob_threshold: float = .5,
        rel_dur_change: Optional[float] = .5,
        abs_dur_change: Optional[float] = None,
        word_level: bool = True,
        precision: float = None,
        single_batch: bool = False,
        inplace: bool = True,
        demucs: Union[bool, torch.nn.Module] = False,
        demucs_options: dict = None,
        only_voice_freq: bool = False,
        verbose: Optional[bool] = False
) -> WhisperResult:
    """
    Improve existing timestamps.

    This function iteratively muting portions of the audio and monitoring token probabilities to find the most precise
    timestamps. This "most precise" in this case means the latest start and earliest end of a word that maintains an
    acceptable probability determined by the specified arguments.

    This is useful readjusting timestamps when they start too early or end too late.

    Parameters
    ----------
    model : "Whisper"
        The Whisper ASR model modified instance
    audio : str or np.ndarray or torch.Tensor or bytes
        Path/URL to the audio file, the audio waveform, or bytes of audio file.
        If audio is :class:`np.ndarray` or :class:`torch.Tensor`, the audio must be already at sampled to 16kHz.
    result : stable_whisper.result.WhisperResult
        All timestamps, words, probabilities, and other data from the transcription of ``audio``.
    steps : str, default 'se'
        Instructions for refinement. A 's' means refine start-timestamps. An 'e' means refine end-timestamps.
    rel_prob_decrease : float, default 0.3
        Maximum percent decrease in probability relative to original probability which is the probability from muting
        according initial timestamps.
    abs_prob_decrease : float, default 0.05
        Maximum decrease in probability from original probability.
    rel_rel_prob_decrease : float, optional
        Maximum percent decrease in probability relative to previous probability which is the probability from previous
        iteration of muting.
    prob_threshold : float, default 0.5
        Stop refining the timestamp if the probability of its token goes below this value.
    rel_dur_change : float, default 0.5
        Maximum percent change in duration of a word relative to its original duration.
    abs_dur_change : float, optional
        Maximum seconds a word is allowed deviate from its original duration.
    word_level : bool, default True
        Whether to refine timestamps on word-level. If ``False``, only refine start/end timestamps of each segment.
    precision : float, default 0.1
        Precision of refined timestamps in seconds. The lowest precision is 0.02 second.
    single_batch : bool, default False
        Whether to process in only batch size of one to reduce memory usage.
    inplace : bool, default True, meaning return a deepcopy of ``result``
        Whether to alter timestamps in-place.
    demucs : bool or torch.nn.Module, default False
        Whether to preprocess ``audio`` with Demucs to isolate vocals / remove noise. Set ``demucs`` to an instance of
        a Demucs model to avoid reloading the model for each run.
        Demucs must be installed to use. Official repo, https://github.com/facebookresearch/demucs.
    demucs_options : dict, optional
        Options to use for :func:`stable_whisper.audio.demucs_audio`.
    only_voice_freq : bool, default False
        Whether to only use sound between 200 - 5000 Hz, where majority of human speech are.
    verbose : bool or None, default False
        Whether to display the text being decoded to the console.
        Displays all the details if ``True``. Displays progressbar if ``False``. Display nothing if ``None``.

    Returns
    -------
    stable_whisper.result.WhisperResult
        All timestamps, words, probabilities, and other data from the refinement of ``text`` with ``audio``.

    Notes
    -----
    The lower the ``precision``, the longer the processing time.

    Examples
    --------
    >>> import stable_whisper
    >>> model = stable_whisper.load_model('base')
    >>> result = model.transcribe('audio.mp3')
    >>> model.refine('audio.mp3', result)
    >>> result.to_srt_vtt('audio.srt')
    Saved 'audio.srt'
    """
    if not steps:
        steps = 'se'
    if precision is None:
        precision = 0.1
    if invalid_steps := steps.replace('s', '').replace('e', ''):
        raise ValueError(f'Invalid step(s): {", ".join(invalid_steps)}')
    if not result.has_words:
        raise NotImplementedError(f'Result must have word timestamps.')

    if not inplace:
        result = copy.deepcopy(result)

    audio = prep_audio(
        audio,
        demucs=demucs,
        demucs_options=demucs_options,
        only_voice_freq=only_voice_freq,
        verbose=verbose
    )
    max_inference_tokens = model.dims.n_text_ctx - 6
    sample_padding = int(N_FFT // 2) + 1
    frame_precision = max(round(precision * FRAMES_PER_SECOND), 2)
    total_duration = round(audio.shape[-1] / SAMPLE_RATE, 3)
    tokenizer = get_tokenizer(model.is_multilingual, language=result.language, task='transcribe')

    def ts_to_frames(timestamps: Union[np.ndarray, list]) -> np.ndarray:
        if isinstance(timestamps, list):
            timestamps = np.array(timestamps)
        return (timestamps * FRAMES_PER_SECOND).round().astype(int)

    def curr_segments():
        all_words = result.all_words()
        seg_edge_mask = np.array([
            1 if _i == 0 else (2 if _i == len(seg.words)-1 else 0)
            for seg in result.segments
            for _i, w in enumerate(seg.words)
        ])
        start_times = [
            max(
                0 if abs_dur_change is None else (w.start - abs_dur_change),
                0 if rel_dur_change is None else (w.start - w.duration * rel_dur_change),
                0 if i == 0 else max(all_words[i - 1].end, w.end - 14.5, 0)
            )
            for i, w in enumerate(all_words)
        ]
        end_times = [
            min(
                total_duration if abs_dur_change is None else (w.end + abs_dur_change),
                total_duration if rel_dur_change is None else (w.end + w.duration * rel_dur_change),
                total_duration if i == len(all_words) else min(all_words[i].start, w.start + 14.5, total_duration)
            )
            for i, w in enumerate(all_words, 1)
        ]
        start = start_times[0]

        prev_i = 0
        curr_words, curr_starts, curr_ends = [], [], []

        for i, w in enumerate(all_words, 1):
            if (
                    (end_times[0] - start > 30) or
                    (len(curr_words) + 1 > max_inference_tokens)
            ):
                if curr_words:
                    yield curr_words, curr_starts, curr_ends, seg_edge_mask[prev_i:prev_i+len(curr_words)]
                    curr_words, curr_starts, curr_ends = [], [], []
                start = start_times[0]
                prev_i = i - 1

            curr_words.append(w)
            curr_starts.append(start_times.pop(0))
            curr_ends.append(end_times.pop(0))

            if i == len(all_words):
                yield curr_words, curr_starts, curr_ends, seg_edge_mask[prev_i:prev_i+len(curr_words)]

    def _refine(_step: str):

        for words, min_starts, max_ends, edge_mask in curr_segments():

            time_offset = min_starts[0]
            start_sample = round(time_offset * SAMPLE_RATE)
            end_sample = round(max_ends[-1] * SAMPLE_RATE)
            audio_segment = audio[start_sample:end_sample + 1].unsqueeze(0)

            max_starts = ts_to_frames(np.array([w.end for w in words]) - time_offset)
            min_ends = ts_to_frames(np.array([w.start for w in words]) - time_offset)
            min_starts = ts_to_frames(np.array(min_starts) - time_offset)
            max_ends = ts_to_frames(np.array(max_ends) - time_offset)

            mid_starts = min_starts + ((max_starts - min_starts) / 2).round().astype(int)
            mid_ends = min_ends + ((max_ends - min_ends) / 2).round().astype(int)

            text_tokens = [t for w in words for t in w.tokens if t < tokenizer.eot]
            word_tokens = [[t for t in w.tokens if t < tokenizer.eot] for w in words]
            orig_mel_segment = log_mel_spectrogram(audio_segment, padding=sample_padding)
            orig_mel_segment = pad_or_trim(orig_mel_segment, N_FRAMES).to(device=model.device)

            def get_prob():

                tokens = torch.tensor(
                    [
                        *tokenizer.sot_sequence,
                        tokenizer.no_timestamps,
                        *text_tokens,
                        tokenizer.eot,
                    ]
                ).to(model.device)

                with torch.no_grad():
                    curr_mel_segment = mel_segment if prob_indices else orig_mel_segment
                    if single_batch:
                        logits = torch.cat(
                            [model(_mel.unsqueeze(0), tokens.unsqueeze(0)) for _mel in curr_mel_segment]
                        )
                    else:
                        logits = model(curr_mel_segment, tokens.unsqueeze(0))

                sampled_logits = logits[:, len(tokenizer.sot_sequence):, : tokenizer.eot]
                token_probs = sampled_logits.softmax(dim=-1)

                text_token_probs = token_probs[:, np.arange(len(text_tokens)), text_tokens]
                token_positions = token_probs[:, np.arange(len(text_tokens))]
                if logits.shape[0] != 1 and prob_indices is not None:
                    indices1 = np.arange(len(prob_indices))
                    text_token_probs = text_token_probs[prob_indices, indices1]
                    token_positions = token_positions[prob_indices, indices1]
                else:
                    text_token_probs.squeeze_(0)

                text_token_probs = text_token_probs.tolist()
                token_positions = \
                    (
                            token_positions.sort().indices == tokens[len(tokenizer.sot_sequence) + 1:-1][:, None]
                    ).nonzero()[:, -1].tolist()

                word_boundaries = np.pad(np.cumsum([len(t) for t in word_tokens]), (1, 0))
                word_probabilities = np.array([
                    text_token_probs[j-1] if is_end_ts else text_token_probs[i]
                    for i, j in zip(word_boundaries[:-1], word_boundaries[1:])
                ])
                token_positions = [
                    token_positions[j-1] if is_end_ts else token_positions[i]
                    for i, j in zip(word_boundaries[:-1], word_boundaries[1:])
                ]

                return word_probabilities, token_positions

            def update_ts():
                if not is_finish[idx] or changes[idx, -1] == -1:
                    return
                new_ts = round(time_offset + (changes[idx, -1] / FRAMES_PER_SECOND), 3)
                if changes[idx, 0] and not changes[idx, 1]:
                    if is_end_ts:
                        if new_ts <= words[idx].end:
                            return
                    elif new_ts >= words[idx].start:
                        return
                if not verbose:
                    return
                curr_word = words[idx]
                word_info = (f'[Word="{curr_word.word}"] '
                             f'[Segment ID: {curr_word.segment_id}] '
                             f'[Word ID: {curr_word.id}]')
                if is_end_ts:
                    print(f'End: {words[idx].end} -> {new_ts}  {word_info}')
                    words[idx].end = new_ts
                else:
                    print(f'Start: {words[idx].start} -> {new_ts}  {word_info}')
                    words[idx].start = new_ts

            mel_segment = orig_mel_segment.clone().repeat_interleave(2, 0)
            is_end_ts = _step == 'e'

            prob_indices = []
            is_finish = np.less([w.probability for w in words], prob_threshold)
            is_finish = np.logical_or(is_finish, [w.duration == 0 for w in words])
            if not word_level:
                is_finish[edge_mask != (2 if is_end_ts else 1)] = True
            for idx, _i in enumerate(max_starts if is_end_ts else min_ends):
                row = idx % 2
                prob_indices.extend([row] * len(words[idx].tokens))
                if is_finish[idx]:
                    continue
                if is_end_ts:
                    _p = mel_segment.shape[-1] if idx == len(words)-1 else mid_ends[idx+1]
                    mel_segment[row, :, _i:_p] = 0
                else:
                    _p = 0 if idx == 0 else mid_starts[idx-1]
                    mel_segment[row, :, _p:_i] = 0
            orig_probs, orig_tk_poss = get_prob()
            changes = np.zeros((orig_probs.shape[-1], 3), dtype=int)
            changes[:, -1] = -1
            frame_indices = (mid_ends, max_starts) if is_end_ts else (min_ends, mid_starts)
            for idx, (_s, _e) in enumerate(zip(*frame_indices)):
                row = idx % 2
                if is_finish[idx]:
                    continue
                mel_segment[row, :, _s:_e] = 0

            new_probs = prev_probs = orig_probs
            while not np.all(is_finish):
                probs, tk_poss = get_prob()
                abs_diffs = orig_probs - probs
                rel_diffs = abs_diffs / orig_probs
                rel_change_diffs = (prev_probs - probs) / prev_probs
                prev_probs = probs
                for idx, (abs_diff, rel_diff, rel_change_diff, prob) \
                        in enumerate(zip(abs_diffs, rel_diffs, rel_change_diffs, probs)):
                    if is_finish[idx]:
                        continue
                    if is_end_ts:
                        curr_min, curr_max, curr_mid = min_ends[idx], max_ends[idx], mid_ends[idx]
                    else:
                        curr_min, curr_max, curr_mid = min_starts[idx], max_starts[idx], mid_starts[idx]

                    row = prob_indices[idx]
                    best_tks_changed = orig_tk_poss[idx] > tk_poss[idx]
                    failed_requirements = (
                            abs_diff > abs_prob_decrease or
                            rel_diff > rel_prob_decrease or
                            (rel_rel_prob_decrease is not None and rel_change_diff > rel_rel_prob_decrease) or
                            prob < prob_threshold or
                            best_tks_changed
                    )

                    if failed_requirements:
                        changes[idx][0] = 1
                        if is_end_ts:
                            curr_min = curr_mid
                        else:
                            curr_max = curr_mid
                    else:
                        changes[idx][1] = 1
                        if is_end_ts:
                            curr_max = curr_mid
                        else:
                            curr_min = curr_mid

                    if (new_mid_change := round((curr_max - curr_min) / 2)) < frame_precision:
                        is_finish[idx] = True
                        update_ts()
                        continue

                    new_mid = curr_min + new_mid_change
                    if failed_requirements:
                        if is_end_ts:
                            mel_segment[row, :, curr_min:new_mid] = orig_mel_segment[0, :, curr_min:new_mid]
                        else:
                            mel_segment[row, :, new_mid:curr_max] = orig_mel_segment[0, :, new_mid:curr_max]

                    else:
                        if is_end_ts:
                            mel_segment[row, :, new_mid:curr_max] = 0
                        else:
                            mel_segment[row, :, curr_min:new_mid] = 0

                    if is_end_ts:
                        min_ends[idx], max_ends[idx], mid_ends[idx] = curr_min, curr_max, new_mid
                    else:
                        min_starts[idx], max_starts[idx], mid_starts[idx] = curr_min, curr_max, new_mid
                    if not best_tks_changed:
                        changes[idx][-1] = new_mid
                    new_probs[idx] = prob

            update_pbar(words[-1].end)

    with tqdm(total=round(total_duration, 2), unit='sec', disable=verbose is not False, desc='Refine') as tqdm_pbar:

        def update_pbar(last_ts: float):
            nonlocal prev_ts
            tqdm_pbar.update(round(((last_ts - prev_ts) / len(steps)), 2))
            prev_ts = last_ts

        for step_count, step in enumerate(steps, 1):
            prev_ts = 0
            _refine(step)
            update_pbar(round(tqdm_pbar.total / len(step), 2))
        tqdm_pbar.update(tqdm_pbar.total - tqdm_pbar.n)

    result.update_all_segs_with_words()

    return result


def locate(
        model: "Whisper",
        audio: Union[str, np.ndarray, torch.Tensor, bytes],
        text: Union[str, List[int]],
        language: str,
        count: int = 1,
        duration_window: Union[float, Tuple[float, float]] = 3.0,
        *,
        mode: int = 0,
        start: float = None,
        end: float = None,
        probability_threshold: float = 0.5,
        eots: int = 1,
        max_token_per_seg: int = 20,
        exact_token: bool = False,
        case_sensitive: bool = False,
        verbose: bool = False,
        initial_prompt: str = None,
        suppress_tokens: Union[str, List[int]] = '-1',
        demucs: Union[bool, torch.nn.Module] = False,
        demucs_options: dict = None,
        only_voice_freq: bool = False,
) -> Union[List[Segment], List[dict]]:
    """
    Locate when specific words are spoken in ``audio`` without fully transcribing.

    This is usefully for quickly finding at what time the specify words or phrases are spoken in an audio. Since it
    does not need to transcribe the audio to approximate the time, it is significantly faster transcribing then
    locating the word in the transcript.

    It can also transcribe few seconds around the approximated time to find out what was said around those words or
    confirm if the word was even spoken near that time.

    Parameters
    ----------
    model : whisper.model.Whisper
        An instance of Whisper ASR model.
    audio : str or np.ndarray or torch.Tensor or bytes
        Path/URL to the audio file, the audio waveform, or bytes of audio file.
        If audio is :class:`np.ndarray` or :class:`torch.Tensor`, the audio must be already at sampled to 16kHz.
    text: str or list of int
        Words/phrase or list of tokens to search for in ``audio``.
    language : str
        Language of the ``text``.
    count : int, default 1, meaning stop search after 1 match
        Number of matches to find. Use 0 to look for all.
    duration_window : float or tuple of (float, float), default 3.0, same as (3.0, 3.0)
        Seconds before and after the end timestamp approximations to transcribe after mode 1.
        If tuple pair of values, then the 1st value will be seconds before the end and 2nd value will be seconds after.
    mode : int, default 0
        Mode of search.
        2, Approximates the end timestamp of ``text`` in the audio. This mode does not confirm whether ``text`` is
            spoken at the timestamp
        1, Completes mode 2 then transcribes audio within ``duration_window`` to confirm whether `text` is a match at
            the approximated timestamp by checking if ``text`` at that ``duration_window`` is within
            ``probability_threshold`` or matching the string content if ``text`` with the transcribed text at the
            ``duration_window``.
        0, Completes mode 1 then add word timestamps to the transcriptions of each match.
        Modes from fastest to slowest: 2, 1, 0
    start : float, optional, meaning it starts from 0s
        Seconds into the audio to start searching for ``text``.
    end : float, optional
        Seconds into the audio to stop searching for ``text``.
    probability_threshold : float, default 0.5
        Minimum probability of each token in ``text`` for it to be considered a match.
    eots : int, default 1
        Number of EOTs to reach before stopping transcription at mode 1. When transcription reach a EOT, it usually
        means the end of the segment or audio. Once ``text`` is found in the ``duration_window``, the transcription
        will stop immediately upon reaching a EOT.
    max_token_per_seg : int, default 20
        Maximum number of tokens to transcribe in the ``duration_window`` before stopping.
    exact_token : bool, default False
        Whether to find a match base on the exact tokens that make up ``text``.
    case_sensitive : bool, default False
        Whether to consider the case of ``text`` when matching in string content.
    verbose : bool or None, default False
        Whether to display the text being decoded to the console.
        Displays all the details if ``True``. Displays progressbar if ``False``. Display nothing if ``None``.
    initial_prompt : str, optional
        Text to provide as a prompt for the first window. This can be used to provide, or
        "prompt-engineer" a context for transcription, e.g. custom vocabularies or proper nouns
        to make it more likely to predict those word correctly.
    suppress_tokens : str or list of int, default '-1', meaning suppress special characters except common punctuations
        List of tokens to suppress.
    demucs : bool or torch.nn.Module, default False
        Whether to preprocess ``audio`` with Demucs to isolate vocals / remove noise. Set ``demucs`` to an instance of
        a Demucs model to avoid reloading the model for each run.
        Demucs must be installed to use. Official repo, https://github.com/facebookresearch/demucs.
    demucs_options : dict, optional
        Options to use for :func:`stable_whisper.audio.demucs_audio`.
    only_voice_freq : bool, default False
        Whether to only use sound between 200 - 5000 Hz, where majority of human speech are.

    Returns
    -------
    stable_whisper.result.Segment or list of dict or list of float
        Mode 0, list of instances of :class:`stable_whisper.result.Segment`.
        Mode 1, list of dictionaries with end timestamp approximation of matches and transcribed neighboring words.
        Mode 2, list of timestamps in seconds for each end timestamp approximation.

    Notes
    -----
    For ``text``, the case and spacing matters as 'on', ' on', ' On' are different tokens, therefore chose the one that
    best suits the context (e.g. ' On' to look for it at the beginning of a sentence).

    Use a sufficiently large first value of ``duration_window`` i.e. the value > time it is expected to speak ``text``.

    If ``exact_token = False`` and the string content matches, then ``probability_threshold`` is not used.

    Examples
    --------
    >>> import stable_whisper
    >>> model = stable_whisper.load_model('base')
    >>> matches = model.locate('audio.mp3', 'are', 'English', verbose=True)

    Some words can sound the same but have different spellings to increase of the chance of finding such words use
    ``initial_prompt``.

    >>> matches = model.locate('audio.mp3', ' Nickie', 'English', verbose=True, initial_prompt='Nickie')
    """
    from whisper.timing import median_filter
    from whisper.decoding import DecodingTask, DecodingOptions, SuppressTokens
    from .timing import split_word_tokens

    sample_padding = int(N_FFT // 2) + 1
    sec_per_emb = model.dims.n_audio_ctx / CHUNK_LENGTH
    CHUNK_SAMPLES = round(CHUNK_LENGTH * SAMPLE_RATE)
    if isinstance(duration_window, (float, int)):
        duration_window = [duration_window] * 2
    window_sum = sum(duration_window)
    assert CHUNK_SAMPLES > window_sum, \
        f'Sum of [duration_window] must be less than {CHUNK_SAMPLES}, got {window_sum}'
    adjusted_chunk_size = CHUNK_SAMPLES - round(duration_window[0]*SAMPLE_RATE)
    if initial_prompt:
        initial_prompt = ' ' + initial_prompt.strip()
    task = DecodingTask(model, DecodingOptions(
        language=language, prompt=initial_prompt, suppress_tokens=suppress_tokens, without_timestamps=True,
    ))
    tokenizer = task.tokenizer
    initial_tokens = list(task.initial_tokens)
    text_tokens, text = (tokenizer.encode(text), text) if isinstance(text, str) else (text, tokenizer.decode(text))
    if not exact_token and not case_sensitive:
        text = text.lower()

    tk_suppress_masks = [
        [i for i in fil.suppress_tokens if i < tokenizer.eot]
        for fil in task.logit_filters if isinstance(fil, SuppressTokens)
    ]

    audio = prep_audio(
        audio,
        demucs=demucs,
        demucs_options=demucs_options,
        only_voice_freq=only_voice_freq,
        verbose=verbose
    )
    prev_target_end = None
    found = 0
    if end:
        audio = audio[:round(end * SAMPLE_RATE)]
    seek_sample = round(start * SAMPLE_RATE) if start else 0
    total_samples = audio.shape[-1]

    def _locate():
        nonlocal seek_sample, found
        seek = round(seek_sample / SAMPLE_RATE, 3)
        audio_segment = audio[seek_sample: seek_sample + CHUNK_SAMPLES]
        mel_segment = log_mel_spectrogram(audio_segment, padding=sample_padding)
        mel_segment = pad_or_trim(mel_segment, N_FRAMES).to(device=model.device)

        QKs = [None] * model.dims.n_text_layer
        hooks = [
            block.cross_attn.register_forward_hook(
                lambda _, ins, outs, index=i: QKs.__setitem__(index, outs[-1])
            )
            for i, block in enumerate(model.decoder.blocks)
        ]
        tokens = torch.tensor([initial_tokens + text_tokens]).to(model.device)
        with torch.no_grad():
            audio_features = model.encoder(mel_segment.unsqueeze(0))
            model.decoder(tokens, audio_features)

        for hook in hooks:
            hook.remove()

        weights = torch.cat([QKs[_l][:, _h] for _l, _h in model.alignment_heads.indices().T], dim=0)
        weights = weights.softmax(dim=-1)
        std, mean = torch.std_mean(weights, dim=-2, keepdim=True, unbiased=False)
        weights = (weights - mean) / std
        weights = median_filter(weights, 7)

        matrix = weights.mean(axis=0)
        target_end = round((matrix[-1].argmax()/sec_per_emb).item(), 3)
        found_msg = f'"{text}" ending at ~{format_timestamp(target_end+seek)}' if verbose else ''

        if mode == 2:
            if found_msg:
                safe_print('Unconfirmed:' + found_msg)
            nonlocal prev_target_end
            found += 1
            if (
                    (seek_sample + CHUNK_SAMPLES >= total_samples) or
                    (count and found >= count) or
                    (prev_target_end == target_end)
            ):
                seek_sample = total_samples
            else:
                seek_sample += round(target_end * SAMPLE_RATE)
            prev_target_end = target_end
            return dict(tokens=[], target_end=target_end+seek)

        curr_start = round(max(target_end - duration_window[0], 0.), 3)
        curr_end = round(target_end + duration_window[1], 3)
        start_frame = round(curr_start * FRAMES_PER_SECOND)
        end_frame = round(curr_end * FRAMES_PER_SECOND)
        mel_segment_section = pad_or_trim(mel_segment[..., start_frame:end_frame], N_FRAMES)
        temp_tokens = torch.tensor([initial_tokens]).to(model.device)

        predictions = []

        target_token_idx = 0
        not_end = True
        found_target = False
        curr_eots = 0
        temp_audio_features = model.encoder(mel_segment_section.unsqueeze(0))
        tokens_to_decode = []
        replace_found_tokens = []
        infer_tokens = [temp_tokens[0]]
        kv_cache, hooks = model.install_kv_cache_hooks()
        while not_end:
            with torch.no_grad():
                logits = model.decoder(temp_tokens, temp_audio_features, kv_cache=kv_cache)[0, -1, :tokenizer.eot+1]
            for tks in tk_suppress_masks:
                logits[tks] = -np.inf
            sorted_logits_idxs = logits.sort(dim=-1).indices[-2:]
            best_token = sorted_logits_idxs[-1]
            best_non_eot_token = sorted_logits_idxs[-2] if best_token == tokenizer.eot else best_token

            logits = logits[:tokenizer.eot].softmax(dim=-1)
            if found_target:
                target_word_prob = is_match = None
            else:
                if exact_token:
                    is_match = False
                else:
                    tokens_to_decode.append(best_non_eot_token)
                    temp_text = tokenizer.decode(tokens_to_decode)
                    if not case_sensitive:
                        temp_text = temp_text.lower()
                    if is_match := temp_text.endswith(text):
                        tokens_to_decode = []
                target_word_prob = logits[text_tokens[target_token_idx]].item()
            if (
                    target_word_prob is not None and
                    (
                            target_word_prob >= probability_threshold or
                            best_non_eot_token == text_tokens[target_token_idx] or
                            is_match
                    )
            ):
                if is_match:
                    best_token = best_non_eot_token
                    token_prob = logits[best_token].item()
                    found_target = True
                else:
                    best_token[None] = text_tokens[target_token_idx]
                    if len(replace_found_tokens) or best_non_eot_token != text_tokens[target_token_idx]:
                        replace_found_tokens.append(best_non_eot_token)
                    target_token_idx += 1
                    if target_token_idx == len(text_tokens):
                        found_target = True
                    token_prob = target_word_prob
                if found_target:
                    found += 1
                curr_eots = 0
            else:
                if not found_target:
                    if len(replace_found_tokens):
                        temp_tokens = torch.cat(infer_tokens)[None]
                        temp_tokens = torch.cat(
                            [temp_tokens[..., :-len(replace_found_tokens)],
                             torch.stack(replace_found_tokens)[None]]
                        )
                        replace_found_tokens = []
                        kv_cache.clear()
                    target_token_idx = 0
                if best_token == tokenizer.eot:
                    if curr_eots >= eots or found_target:
                        not_end = False
                    else:
                        curr_eots += 1
                        best_token = best_non_eot_token
                else:
                    curr_eots = 0
                token_prob = None if best_token == tokenizer.eot else logits[best_token].item()

            predictions.append(dict(token=best_token.item(), prob=token_prob))
            if len(predictions) > max_token_per_seg:
                not_end = False
            if not_end:
                infer_tokens.append(best_token[None])
                temp_tokens = best_token[None, None]
        kv_cache.clear()
        for hook in hooks:
            hook.remove()
        segment = None

        if found_target:
            if found_msg:
                safe_print('Confirmed: ' + found_msg)
            final_tokens = [p['token'] for p in predictions]
            if mode == 1:
                _, (ws, wts), _ = split_word_tokens([dict(tokens=final_tokens)], tokenizer)
                final_token_probs = [p['prob'] for p in predictions]
                wps = [float(np.mean([final_token_probs.pop(0) for _ in wt])) for wt in wts]
                words = [dict(word=w, tokens=wt, probability=wp) for w, wt, wp in zip(ws, wts, wps)]
                final_end = target_end+seek
                near_text = "".join(ws)
                segment = dict(end=final_end, text=text, duration_window_text=near_text, duration_window_word=words)
                if verbose:
                    safe_print(f'Duration Window: "{near_text}"\n')
                seek_sample += round(curr_end * SAMPLE_RATE)
            else:

                segment = dict(
                    seek=0,
                    tokens=final_tokens
                )

                add_word_timestamps_stable(
                    segments=[segment],
                    model=model,
                    tokenizer=tokenizer,
                    mel=mel_segment,
                    num_samples=round(curr_end*SAMPLE_RATE),
                    gap_padding=None
                )
                segment = Segment(0, 0, '', words=segment['words'])
                segment.update_seg_with_words()
                seek_sample += round(segment.words[-1].end * SAMPLE_RATE)
                segment.offset_time(seek)
                segment.seek = curr_start
                if verbose:
                    safe_print(segment.to_display_str())

        else:
            seek_sample += adjusted_chunk_size if audio_segment.shape[-1] == CHUNK_SAMPLES else audio_segment.shape[-1]

        return segment

    total_duration = round(total_samples / SAMPLE_RATE, 2)
    matches = []
    with tqdm(total=total_duration, unit='sec', disable=verbose is not False, desc='Locate') as tqdm_pbar:
        while seek_sample < total_samples and (not count or found < count):
            if match := _locate():
                matches.append(match)
            tqdm_pbar.update(round(seek_sample/SAMPLE_RATE, 2) - tqdm_pbar.n)
        tqdm_pbar.update(tqdm_pbar.total - tqdm_pbar.n)
    if verbose and not matches:
        safe_print(f'Failed to locate "{text}".')
    return matches
