# Stabilizing Timestamps for Whisper

This script modifies [OpenAI's Whisper](https://github.com/openai/whisper) to produce more reliable timestamps.

https://user-images.githubusercontent.com/28970749/225826345-ef7115db-51e4-4b23-aedd-069389b8ae43.mp4

* [Setup](#setup)
* [Usage](#usage)
  * [Transcribe](#transcribe)
  * [Output](#output)
  * [Alignment](#alignment)
    * [Adjustments](#adjustments)
  * [Refinement](#refinement)
  * [Regrouping Words](#regrouping-words)
  * [Locating Words](#locating-words)
  * [Tips](#tips)
  * [Visualizing Suppression](#visualizing-suppression)
  * [Encode Comparison](#encode-comparison)
  * [Use with any ASR](#any-asr)
* [Quick 1.X → 2.X Guide](#quick-1x--2x-guide)

## Setup
```
pip install -U stable-ts
```

To install the latest commit:
```
pip install -U git+https://github.com/jianfch/stable-ts.git
```

## Usage

### Transcribe

```python
import stable_whisper
model = stable_whisper.load_model('base')
result = model.transcribe('audio.mp3')
result.to_srt_vtt('audio.srt')
```
<details>
<summary>CLI</summary>

```commandline
stable-ts audio.mp3 -o audio.srt
```

</details>

Parameters: 
[load_model()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/whisper_word_level.py#L985-L1014), 
[transcribe()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/whisper_word_level.py#L74-L211),
[transcribe_minimal()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/whisper_word_level.py#L650-L723)

<details>
<summary>faster-whisper</summary>

Use with [faster-whisper](https://github.com/guillaumekln/faster-whisper):
```python
model = stable_whisper.load_faster_whisper('base')
result = model.transcribe_stable('audio.mp3')
```
Parameters: 
[transcribe_stable()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/whisper_word_level.py#L835-L912), 

</details>

### Output
Stable-ts supports various text output formats.
```python
result.to_srt_vtt('audio.srt') #SRT
result.to_srt_vtt('audio.vtt') #VTT
result.to_ass('audio.ass') #ASS
result.to_tsv('audio.tsv') #TSV
```
Parameters: 
[to_srt_vtt()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/text_output.py#L260-L302),
[to_ass()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/text_output.py#L406-L459),
[to_tsv()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/text_output.py#L334-L372)
[save_as_json()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/text_output.py#L522-L531)
<br /><br />
There are word-level and segment-level timestamps. All output formats support them. 
They also support will both levels simultaneously except TSV. 
By default, `segment_level` and `word_level` are both `True` for all the formats that support both simultaneously.<br /><br />
Examples in VTT.

Default: `segment_level=True` + `word_level=True`
<details>
<summary>CLI</summary>

`--segment_level true` + `--word_level true`

</details>

```
00:00:07.760 --> 00:00:09.900
But<00:00:07.860> when<00:00:08.040> you<00:00:08.280> arrived<00:00:08.580> at<00:00:08.800> that<00:00:09.000> distant<00:00:09.400> world,
```

`segment_level=True`  + `word_level=False`
```
00:00:07.760 --> 00:00:09.900
But when you arrived at that distant world,
```

`segment_level=False` + `word_level=True`
```
00:00:07.760 --> 00:00:07.860
But

00:00:07.860 --> 00:00:08.040
when

00:00:08.040 --> 00:00:08.280
you

00:00:08.280 --> 00:00:08.580
arrived

...
```

#### JSON
The result can also be saved as a JSON file to preserve all the data for future reprocessing. 
This is useful for testing different sets of postprocessing arguments without the need to redo inference.

```python
result.save_as_json('audio.json')
```
<details>
<summary>CLI</summary>

```commandline
stable-ts audio.mp3 -o audio.json
```

</details>

Processing JSON file of the results into SRT.
```python
result = stable_whisper.WhisperResult('audio.json')
result.to_srt_vtt('audio.srt')
```
<details>
<summary>CLI</summary>

```commandline
stable-ts audio.json -o audio.srt
```

</details>

### Alignment
Audio can be aligned/synced with plain text on word-level.
```python
text = 'Machines thinking, breeding. You were to bear us a new, promised land.'
result = model.align('audio.mp3', text)
```
When the text is correct but the timestamps need more work, 
`align()` is a faster alternative for testing various settings/models.
```python
new_result = model.align('audio.mp3', result)
```
<details>
<summary>CLI</summary>

```commandline
stable-ts audio.mp3 --align text.txt --language en
```
`--align` can also a JSON file of a result 

</details>

Parameters:
[align()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/alignment.py#L56-L153)

#### Adjustments
Timestamps are adjusted after the model predicts them. 
When `suppress_silence=True` (default), `transcribe()`/`transcribe_minimal()`/`align()` adjust based on silence/non-speech. 
The timestamps can be further adjusted base on another result with `adjust_by_result()`, 
which acts as a logical AND operation for the timestamps of both results, further reducing duration of each word.
Note: both results are required to have word timestamps and matching words.
```python
# the adjustments are in-place for `result`
result.adjust_by_result(new_result)
```
Parameters:
[adjust_by_result()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/result.py#L754-L765)

### Refinement
Timestamps can be further improved with `refine()`.
This method iteratively mutes portions of the audio based on current timestamps 
then compute the probabilities of the tokens. 
Then by monitoring the fluctuation of the probabilities, it tries to find the most precise timestamps. 
"Most precise" in this case means the latest start and earliest end for the word 
such that it still meets the specified conditions.
```python
model.refine('audio.mp3', result)
```
<details>
<summary>CLI</summary>

```commandline
stable-ts audio.mp3 --refine -o audio.srt
```
Input can also be JSON file of a result. 
```commandline
stable-ts result.json --refine -o audio.srt --refine_option "audio=audio.mp3"
```

</details>

Parameters:
[refine()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/alignment.py#L348-L419)


### Regrouping Words
Stable-ts has a preset for regrouping words into different segments with more natural boundaries. 
This preset is enabled by `regroup=True` (default). 
But there are other built-in [regrouping methods](#regrouping-methods) that allow you to customize the regrouping algorithm. 
This preset is just a predefined combination of those methods.

https://user-images.githubusercontent.com/28970749/226504985-3d087539-cfa4-46d1-8eb5-7083f235b429.mp4

```python
# The following results are all functionally equivalent:
result0 = model.transcribe('audio.mp3', regroup=True) # regroup is True by default
result1 = model.transcribe('audio.mp3', regroup=False)
(
    result1
    .clamp_max()
    .split_by_punctuation([('.', ' '), '。', '?', '？', (',', ' '), '，'])
    .split_by_gap(.5)
    .merge_by_gap(.3, max_words=3)
    .split_by_punctuation([('.', ' '), '。', '?', '？'])
)
result2 = model.transcribe('audio.mp3', regroup='cm_sp=.* /。/?/？/,* /，_sg=.5_mg=.3+3_sp=.* /。/?/？')

# To undo all regrouping operations:
result0.reset()
```
Any regrouping algorithm can be expressed as a string. Please feel free share your strings [here](https://github.com/jianfch/stable-ts/discussions/162)
#### Regrouping Methods
- [regroup()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/result.py#L1226-L1277)
- [split_by_gap()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/result.py#L923-L937)
- [split_by_punctuation()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/result.py#L981-L995)
- [split_by_length()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/result.py#L1055-L1084)
- [merge_by_gap()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/result.py#L948-L970)
- [merge_by_punctuation()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/result.py#L1007-L1028)
- [merge_all_segments()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/result.py#L1035-L1042)
- [clamp_max()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/result.py#L1105-L1127)
- [lock()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/result.py#L1167-L1190)

### Locating Words
There are two ways to locate words. 
The first way is by approximating time at which the words are spoken 
then transcribing a few seconds around that approximating times as need.
This also the faster way for locating words.
```python
matches = model.locate('audio.mp3', 'are', 'English')
for match in matches:
    print(match.to_display_str())
# verbose=True does the same thing as this for-loop.
```
Parameters:
[locate()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/alignment.py#L728-L824)

The second way allows you to locate words with regular expression,
but it requires the audio to be fully transcribed first. 
```python
result = model.transcribe('audio.mp3')
# Find every sentence that contains "and"
matches = result.find(r'[^.]+and[^.]+\.')
# print the all matches if there are any
for match in matches:
  print(f'match: {match.text_match}\n'
        f'text: {match.text}\n'
        f'start: {match.start}\n'
        f'end: {match.end}\n')
  
# Find the word before and after "and" in the matches
matches = matches.find(r'\s\S+\sand\s\S+')
for match in matches:
  print(f'match: {match.text_match}\n'
        f'text: {match.text}\n'
        f'start: {match.start}\n'
        f'end: {match.end}\n')
```
Parameters: 
[find()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/result.py#L1314-L1330)

### Tips
- do not disable word timestamps with `word_timestamps=False` for reliable segment timestamps
- use `vad=True` for more accurate non-speech detection
- use `demucs=True` to isolate vocals with [Demucs](https://github.com/facebookresearch/demucs); it is also effective at isolating vocals even if there is no music
- use `demucs=True` and `vad=True` for music
- `--dq true` or `dq=True` for `stable_whisper.load_model` to enable dynamic quantization for inference on CPU
- use `encode_video_comparison()` to encode multiple transcripts into one video for synced comparison; see [Encode Comparison](#encode-comparison) 
- use `visualize_suppression()` to visualize the differences between non-VAD and VAD options; see [Visualizing Suppression](#visualizing-suppression)
- if the non-speech/silence seems to be detected but the starting timestamps do not reflect that, then try `min_word_dur=0`
- [refinement](#refinement) is a great alternative to silence suppression (e.g. if VAD isn't effective)

### Visualizing Suppression
You can visualize which parts of the audio will likely be suppressed (i.e. marked as silent). 
Requires: [Pillow](https://github.com/python-pillow/Pillow) or [opencv-python](https://github.com/opencv/opencv-python).

#### Without VAD
```python
import stable_whisper
# regions on the waveform colored red are where it will likely be suppressed and marked as silent
# [q_levels]=20 and [k_size]=5 (default)
stable_whisper.visualize_suppression('audio.mp3', 'image.png', q_levels=20, k_size = 5) 
```
![novad](https://user-images.githubusercontent.com/28970749/225825408-aca63dbf-9571-40be-b399-1259d98f93be.png)

#### With [Silero VAD](https://github.com/snakers4/silero-vad)
```python
# [vad_threshold]=0.35 (default)
stable_whisper.visualize_suppression('audio.mp3', 'image.png', vad=True, vad_threshold=0.35)
```
![vad](https://user-images.githubusercontent.com/28970749/225825446-980924a5-7485-41e1-b0d9-c9b069d605f2.png)
Parameters: 
[visualize_suppression()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/stabilization.py#L344-L373)

### Encode Comparison
You can encode videos similar to the ones in the doc for comparing transcriptions of the same audio. 
```python
stable_whisper.encode_video_comparison(
    'audio.mp3', 
    ['audio_sub1.srt', 'audio_sub2.srt'], 
    output_videopath='audio.mp4', 
    labels=['Example 1', 'Example 2']
)
```
Parameters: 
[encode_video_comparison()](https://github.com/jianfch/stable-ts/blob/main/stable_whisper/video_output.py#L29-L73)

#### Multiple Files with CLI 
Transcribe multiple audio files then process the results directly into SRT files.
```commandline
stable-ts audio1.mp3 audio2.mp3 audio3.mp3 -o audio1.srt audio2.srt audio3.srt
```

### Any ASR
You can use most of the features of Stable-ts improve the results of any ASR model/APIs. 
[Just follow this notebook](https://github.com/jianfch/stable-ts/blob/main/examples/non-whisper.ipynb).

## Quick 1.X → 2.X Guide
### What's new in 2.0.0?
- updated to use Whisper's more reliable word-level timestamps method. 
- the more reliable word timestamps allow regrouping all words into segments with more natural boundaries.
- can now suppress silence with [Silero VAD](https://github.com/snakers4/silero-vad) (requires PyTorch 1.12.0+)
- non-VAD silence suppression is also more robust
### Usage changes
- `results_to_sentence_srt(result, 'audio.srt')` → `result.to_srt_vtt('audio.srt', word_level=False)` 
- `results_to_word_srt(result, 'audio.srt')` → `result.to_srt_vtt('output.srt', segment_level=False)`
- `results_to_sentence_word_ass(result, 'audio.srt')` → `result.to_ass('output.ass')`
- there's no need to stabilize segments after inference because they're already stabilized during inference
- `transcribe()` returns a `WhisperResult` object which can be converted to `dict` with `.to_dict()`. e.g `result.to_dict()`

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details

## Acknowledgments
Includes slight modification of the original work: [Whisper](https://github.com/openai/whisper)
