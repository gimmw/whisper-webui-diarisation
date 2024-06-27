import textwrap
import unicodedata
import re

import zlib
from typing import Iterator, TextIO, Union
import tqdm

import urllib3


def exact_div(x, y):
    assert x % y == 0
    return x // y


def str2bool(string):
    str2val = {"True": True, "False": False}
    if string in str2val:
        return str2val[string]
    else:
        raise ValueError(f"Expected one of {set(str2val.keys())}, got {string}")


def optional_int(string):
    return None if string == "None" else int(string)


def optional_float(string):
    return None if string == "None" else float(string)


def compression_ratio(text) -> float:
    return len(text) / len(zlib.compress(text.encode("utf-8")))


def format_timestamp(seconds: float, always_include_hours: bool = False, fractionalSeperator: str = '.'):
    assert seconds >= 0, "non-negative timestamp expected"
    milliseconds = round(seconds * 1000.0)

    hours = milliseconds // 3_600_000
    milliseconds -= hours * 3_600_000

    minutes = milliseconds // 60_000
    milliseconds -= minutes * 60_000

    seconds = milliseconds // 1_000
    milliseconds -= seconds * 1_000

    hours_marker = f"{hours:02d}:" if always_include_hours or hours > 0 else ""
    return f"{hours_marker}{minutes:02d}:{seconds:02d}{fractionalSeperator}{milliseconds:03d}"


def write_txt(transcript: Iterator[dict], file: TextIO):
    for segment in transcript:
        print(segment['text'].strip(), file=file, flush=True)

def write_html(transcript: Iterator[dict], file: TextIO, maxLineWidth=None, highlight_words: bool = False):
  
    print("<html>", file=file)
    #print inline css
    css = """
      body {
        color-scheme: light dark;
        color: #ffffffde;
        background-color: #242424;
      }
      
      span.timestamp {
        font-family: monospace;
      }
      
      div.segment {
        display: flex;
        align-items: center;
        gap: 1rem;
        text-align: left;
      }
      
      .speaker.s00 {
        color: #59ffa1;
      }
      
      .speaker.s01 {
        color: #597aff;
      }
      
      .speaker.s02 {
        color: #ff9595;
      }
      
      .speaker.s03 {
        color: #e29b16;
      }
      
      .speaker.s04 {
        color: #e29b16;
      }
      
      .speaker.s05 {
        color: #8316e2;
      }
      
      .speaker.s06 {
        color: #8316e2;
      }
      
      .speaker.s07 {
        color: #16e29b;
      }
      
      .speaker.s08 {
        color: #e23f16;
      }
      
      .speaker.s09 {
        color: #e23f16;
      }
      
      .speaker.s10 {
        color: #16bae2;
      }
      
      .speaker.s11 {
        color: #a9e216;
      }
      
      .speaker.s12 {
        color: #1676e2;
      }
      
      .speaker.s13 {
        color: #163fe2;
      }
      
      .speaker.s14 {
        color: #16bae2;
      }
      
      .speaker.s15 {
        color: #e2165e;
      }
      
      .speaker.s16 {
        color: #e23516;
      }
      
      .speaker.s17 {
        color: #16e287;
      }
      
      .speaker.s18 {
        color: #16bae2;
      }
      
      .speaker.s19 {
        color: #e2a216;
      }
    """
    print (f"\t<head>\t\t<style>{css}\t\t</style>\t</head>", file=file)
    print("\t<body>\n", file=file)
    print("\t\t<div class='segments'>\n", file=file)
    for segment in transcript:
      try:
        #text = re.sub(r'\(SPEAKER_\d+\)', '', segment['text'])
        text = segment['text'].strip()
        #segment_longest_speaker = segment.get('longest_speaker', '')
        segment_longest_speaker = segment.get('longest_speaker', None)

        if match := re.search('SPEAKER_(\d+)', segment_longest_speaker):
          speakerid = match.group(1)
        else:
          speakerid = ""
        
        
        print(
                f"\t\t\t<div class=\"segment\">\n\t\t<span class=\"timestamp\">{format_timestamp(segment['start'])}</span>\n"
                f"\t\t\t\t<span class=\"speaker s{speakerid}\">{speakerid}</span>\n"
                f"\t\t\t\t<p class=\"speaker s{speakerid}\">{text}</p>\n"
                f"\t\t\t</div>\n",
                file=file,
                flush=True,
            )
      except Exception as e:
        print(f"HTML:Error writing segment {segment}: {e}")
        raise
    print("\t\t</div>\n", file=file)
    print("\t</body></html>", file=file)

def write_vtt(transcript: Iterator[dict], file: TextIO, 
              maxLineWidth=None, highlight_words: bool = False):
    iterator  = __subtitle_preprocessor_iterator(transcript, maxLineWidth, highlight_words)

    print("WEBVTT\n", file=file)

    for segment in iterator:
        try:
            text = segment['text'].replace('-->', '->')

            print(
                f"{format_timestamp(segment['start'])} --> {format_timestamp(segment['end'])}\n"
                f"{text}\n",
                file=file,
                flush=True,
            )
        except Exception as e:
            print(f"Error writing segment {segment}: {e}")
            raise

def write_srt(transcript: Iterator[dict], file: TextIO, 
              maxLineWidth=None, highlight_words: bool = False):
    """
    Write a transcript to a file in SRT format.
    Example usage:
        from pathlib import Path
        from whisper.utils import write_srt
        result = transcribe(model, audio_path, temperature=temperature, **args)
        # save SRT
        audio_basename = Path(audio_path).stem
        with open(Path(output_dir) / (audio_basename + ".srt"), "w", encoding="utf-8") as srt:
            write_srt(result["segments"], file=srt)
    """
    iterator  = __subtitle_preprocessor_iterator(transcript, maxLineWidth, highlight_words)

    for i, segment in enumerate(iterator, start=1):
        text = segment['text'].replace('-->', '->')

        # write srt lines
        print(
            f"{i}\n"
            f"{format_timestamp(segment['start'], always_include_hours=True, fractionalSeperator=',')} --> "
            f"{format_timestamp(segment['end'], always_include_hours=True, fractionalSeperator=',')}\n"
            f"{text}\n",
            file=file,
            flush=True,
        )

def __subtitle_preprocessor_iterator(transcript: Iterator[dict], maxLineWidth: int = None, highlight_words: bool = False): 
    for segment in transcript:
        words: list = segment.get('words', [])

        # Append longest speaker ID if available
        segment_longest_speaker = segment.get('longest_speaker', None)

        if len(words) == 0:
            # Yield the segment as-is or processed
            if (maxLineWidth is None or maxLineWidth < 0) and segment_longest_speaker is None:
                yield segment
            else:
                text = segment['text'].strip()

                # Prepend the longest speaker ID if available
                if segment_longest_speaker is not None:
                    text = f"({segment_longest_speaker}) {text}"

                yield {
                    'start': segment['start'],
                    'end': segment['end'],
                    'text': process_text(text, maxLineWidth)
                }
            # We are done
            continue

        subtitle_start = segment['start']
        subtitle_end = segment['end']

        if segment_longest_speaker is not None:
            # Add the beginning
            words.insert(0, {
                'start': subtitle_start,
                'end': subtitle_start,
                'word': f"({segment_longest_speaker})"
            })

        text_words = [ this_word["word"] for this_word in words ]
        subtitle_text = __join_words(text_words, maxLineWidth)

        # Iterate over the words in the segment
        if highlight_words:
            last = subtitle_start

            for i, this_word in enumerate(words):
                start = this_word['start']
                end = this_word['end']

                if last != start:
                    # Display the text up to this point
                    yield {
                        'start': last,
                        'end': start,
                        'text': subtitle_text
                    }
                
                # Display the text with the current word highlighted
                yield {
                    'start': start,
                    'end': end,
                    'text': __join_words(
                        [
                            {
                                "word": re.sub(r"^(\s*)(.*)$", r"\1<u>\2</u>", word)
                                        if j == i
                                        else word,
                                # The HTML tags <u> and </u> are not displayed, 
                                # # so they should not be counted in the word length
                                "length": len(word)
                            } for j, word in enumerate(text_words)
                        ], maxLineWidth)
                }
                last = end

            if last != subtitle_end:
                # Display the last part of the text
                yield {
                    'start': last,
                    'end': subtitle_end,
                    'text': subtitle_text
                }

        # Just return the subtitle text
        else:
            yield {
                'start': subtitle_start,
                'end': subtitle_end,
                'text': subtitle_text
            }

def __join_words(words: Iterator[Union[str, dict]], maxLineWidth: int = None):
    if maxLineWidth is None or maxLineWidth < 0:
        return " ".join(words)
    
    lines = []
    current_line = ""
    current_length = 0

    for entry in words:
        # Either accept a string or a dict with a 'word' and 'length' field
        if isinstance(entry, dict):
            word = entry['word']
            word_length = entry['length']
        else:
            word = entry
            word_length = len(word)

        if current_length > 0 and current_length + word_length > maxLineWidth:
            lines.append(current_line)
            current_line = ""
            current_length = 0
        
        current_length += word_length
        # The word will be prefixed with a space by Whisper, so we don't need to add one here
        current_line += word

    if len(current_line) > 0:
        lines.append(current_line)

    return "\n".join(lines)

def process_text(text: str, maxLineWidth=None):
    if (maxLineWidth is None or maxLineWidth < 0):
        return text

    lines = textwrap.wrap(text, width=maxLineWidth, tabsize=4)
    return '\n'.join(lines)

def slugify(value, allow_unicode=False):
    """
    Taken from https://github.com/django/django/blob/master/django/utils/text.py
    Convert to ASCII if 'allow_unicode' is False. Convert spaces or repeated
    dashes to single dashes. Remove characters that aren't alphanumerics,
    underscores, or hyphens. Convert to lowercase. Also strip leading and
    trailing whitespace, dashes, and underscores.
    """
    value = str(value)
    if allow_unicode:
        value = unicodedata.normalize('NFKC', value)
    else:
        value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value.lower())
    return re.sub(r'[-\s]+', '-', value).strip('-_')

def download_file(url: str, destination: str):
        with urllib3.request.urlopen(url) as source, open(destination, "wb") as output:
            with tqdm(
                total=int(source.info().get("Content-Length")),
                ncols=80,
                unit="iB",
                unit_scale=True,
                unit_divisor=1024,
            ) as loop:
                while True:
                    buffer = source.read(8192)
                    if not buffer:
                        break

                    output.write(buffer)
                    loop.update(len(buffer))
