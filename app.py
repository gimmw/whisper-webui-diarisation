from datetime import datetime
import json
import math
from typing import Callable, Iterator, Union
import argparse

from io import StringIO
import os
import pathlib
import tempfile
import zipfile
import numpy as np

import torch

from src.config import VAD_INITIAL_PROMPT_MODE_VALUES, ApplicationConfig, VadInitialPromptMode
from src.diarization.diarization import Diarization
from src.diarization.diarizationContainer import DiarizationContainer
from src.diarization.transcriptLoader import load_transcript
from src.hooks.progressListener import ProgressListener
from src.hooks.subTaskProgressListener import SubTaskProgressListener
from src.languages import get_language_names
from src.modelCache import ModelCache
from src.prompts.jsonPromptStrategy import JsonPromptStrategy
from src.prompts.prependPromptStrategy import PrependPromptStrategy
from src.source import AudioSource, get_audio_source_collection
from src.vadParallel import ParallelContext, ParallelTranscription

# External programs
import ffmpeg

# UI
import gradio as gr

from src.download import ExceededMaximumDuration, download_url
from src.utils import optional_int, slugify, str2bool, write_srt, write_vtt, write_html
from src.vad import AbstractTranscription, NonSpeechStrategy, PeriodicTranscriptionConfig, TranscriptionConfig, VadPeriodicTranscription, VadSileroTranscription
from src.whisper.abstractWhisperContainer import AbstractWhisperContainer
from src.whisper.whisperFactory import create_whisper_container

# Configure more application defaults in config.json5

# Gradio seems to truncate files without keeping the extension, so we need to truncate the file prefix ourself 
MAX_FILE_PREFIX_LENGTH = 17

# Limit auto_parallel to a certain number of CPUs (specify vad_cpu_cores to get a higher number)
MAX_AUTO_CPU_CORES = 8

WHISPER_MODELS = ["tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en", "large", "large-v1", "large-v2"]

class VadOptions:
    def __init__(self, vad: str = None, vadMergeWindow: float = 5, vadMaxMergeSize: float = 150, vadPadding: float = 1, vadPromptWindow: float = 1, 
                                        vadInitialPromptMode: Union[VadInitialPromptMode, str] = VadInitialPromptMode.PREPREND_FIRST_SEGMENT):
        self.vad = vad
        self.vadMergeWindow = vadMergeWindow
        self.vadMaxMergeSize = vadMaxMergeSize
        self.vadPadding = vadPadding
        self.vadPromptWindow = vadPromptWindow
        self.vadInitialPromptMode = vadInitialPromptMode if isinstance(vadInitialPromptMode, VadInitialPromptMode) \
                                        else VadInitialPromptMode.from_string(vadInitialPromptMode)

class WhisperTranscriber:
    def __init__(self, input_audio_max_duration: float = None, vad_process_timeout: float = None, 
                 vad_cpu_cores: int = 1, delete_uploaded_files: bool = False, output_dir: str = None, 
                 app_config: ApplicationConfig = None):
        self.model_cache = ModelCache()
        self.parallel_device_list = None
        self.gpu_parallel_context = None
        self.cpu_parallel_context = None
        self.vad_process_timeout = vad_process_timeout
        self.vad_cpu_cores = vad_cpu_cores

        self.vad_model = None
        self.inputAudioMaxDuration = input_audio_max_duration
        self.deleteUploadedFiles = delete_uploaded_files
        self.output_dir = output_dir

        # Support for diarization
        self.diarization: DiarizationContainer = None
        # Dictionary with parameters to pass to diarization.run - if None, diarization is not enabled
        self.diarization_kwargs = None
        self.app_config = app_config

    def set_parallel_devices(self, vad_parallel_devices: str):
        self.parallel_device_list = [ device.strip() for device in vad_parallel_devices.split(",") ] if vad_parallel_devices else None

    def set_auto_parallel(self, auto_parallel: bool):
        if auto_parallel:
            if torch.cuda.is_available():
                self.parallel_device_list = [ str(gpu_id) for gpu_id in range(torch.cuda.device_count())]

            self.vad_cpu_cores = min(os.cpu_count(), MAX_AUTO_CPU_CORES)
            print("[Auto parallel] Using GPU devices " + str(self.parallel_device_list) + " and " + str(self.vad_cpu_cores) + " CPU cores for VAD/transcription.")

    def set_diarization(self, auth_token: str, enable_daemon_process: bool = True, **kwargs):
        if self.diarization is None:
            self.diarization = DiarizationContainer(auth_token=auth_token, enable_daemon_process=enable_daemon_process, 
                                                    auto_cleanup_timeout_seconds=self.app_config.diarization_process_timeout, 
                                                    cache=self.model_cache)
        # Set parameters
        self.diarization_kwargs = kwargs

    def unset_diarization(self):
        if self.diarization is not None:
            self.diarization.cleanup()
        self.diarization_kwargs = None

    # Entry function for the simple tab
    def transcribe_webui_simple(self, modelName, languageName, urlData, multipleFiles, microphoneData, task, 
                                vad, vadMergeWindow, vadMaxMergeSize, 
                                word_timestamps: bool = False, highlight_words: bool = False,
                                diarization: bool = False, diarization_speakers: int = None):
        return self.transcribe_webui_simple_progress(modelName, languageName, urlData, multipleFiles, microphoneData, task, 
                                                     vad, vadMergeWindow, vadMaxMergeSize, 
                                                     word_timestamps, highlight_words, 
                                                     diarization, diarization_speakers)
    
    # Entry function for the simple tab progress
    def transcribe_webui_simple_progress(self, modelName, languageName, urlData, multipleFiles, microphoneData, task, 
                                         vad, vadMergeWindow, vadMaxMergeSize, 
                                         word_timestamps: bool = False, highlight_words: bool = False, 
                                         diarization: bool = False, diarization_speakers: int = None,
                                         progress=gr.Progress()):
        
        vadOptions = VadOptions(vad, vadMergeWindow, vadMaxMergeSize, self.app_config.vad_padding, self.app_config.vad_prompt_window, self.app_config.vad_initial_prompt_mode)

        if diarization:
            self.set_diarization(auth_token=self.app_config.auth_token, num_speakers=diarization_speakers if diarization_speakers > 0 else None)
        else:
            self.unset_diarization()

        return self.transcribe_webui(modelName, languageName, urlData, multipleFiles, microphoneData, task, vadOptions, 
                                     word_timestamps=word_timestamps, highlight_words=highlight_words, progress=progress)

    # Entry function for the full tab
    def transcribe_webui_full(self, modelName, languageName, urlData, multipleFiles, microphoneData, task, 
                              vad, vadMergeWindow, vadMaxMergeSize, vadPadding, vadPromptWindow, vadInitialPromptMode, 
                              # Word timestamps
                              word_timestamps: bool, highlight_words: bool, prepend_punctuations: str, append_punctuations: str,
                              initial_prompt: str, temperature: float, best_of: int, beam_size: int, patience: float, length_penalty: float, suppress_tokens: str, 
                              condition_on_previous_text: bool, fp16: bool, temperature_increment_on_fallback: float, 
                              compression_ratio_threshold: float, logprob_threshold: float, no_speech_threshold: float,
                              diarization: bool = False, diarization_speakers: int = None, 
                              diarization_min_speakers = 1, diarization_max_speakers = 5):
        
        return self.transcribe_webui_full_progress(modelName, languageName, urlData, multipleFiles, microphoneData, task, 
                                vad, vadMergeWindow, vadMaxMergeSize, vadPadding, vadPromptWindow, vadInitialPromptMode,
                                word_timestamps, highlight_words, prepend_punctuations, append_punctuations,
                                initial_prompt, temperature, best_of, beam_size, patience, length_penalty, suppress_tokens,
                                condition_on_previous_text, fp16, temperature_increment_on_fallback,
                                compression_ratio_threshold, logprob_threshold, no_speech_threshold,
                                diarization, diarization_speakers, 
                                diarization_min_speakers, diarization_max_speakers)

    # Entry function for the full tab with progress
    def transcribe_webui_full_progress(self, modelName, languageName, urlData, multipleFiles, microphoneData, task, 
                                        vad, vadMergeWindow, vadMaxMergeSize, vadPadding, vadPromptWindow, vadInitialPromptMode,
                                        # Word timestamps
                                        word_timestamps: bool, highlight_words: bool, prepend_punctuations: str, append_punctuations: str,   
                                        initial_prompt: str, temperature: float, best_of: int, beam_size: int, patience: float, length_penalty: float, suppress_tokens: str, 
                                        condition_on_previous_text: bool, fp16: bool, temperature_increment_on_fallback: float, 
                                        compression_ratio_threshold: float, logprob_threshold: float, no_speech_threshold: float, 
                                        diarization: bool = False, diarization_speakers: int = None, 
                                        diarization_min_speakers = 1, diarization_max_speakers = 5,
                                        progress=gr.Progress()):

        # Handle temperature_increment_on_fallback
        if temperature_increment_on_fallback is not None:
            temperature = tuple(np.arange(temperature, 1.0 + 1e-6, temperature_increment_on_fallback))
        else:
            temperature = [temperature]

        vadOptions = VadOptions(vad, vadMergeWindow, vadMaxMergeSize, vadPadding, vadPromptWindow, vadInitialPromptMode)

        # Set diarization
        if diarization:
            self.set_diarization(auth_token=self.app_config.auth_token, num_speakers=diarization_speakers if diarization_speakers > 0 else None, 
                                 min_speakers=diarization_min_speakers, max_speakers=diarization_max_speakers)
        else:
            self.unset_diarization()

        return self.transcribe_webui(modelName, languageName, urlData, multipleFiles, microphoneData, task, vadOptions,
                                     initial_prompt=initial_prompt, temperature=temperature, best_of=best_of, beam_size=beam_size, patience=patience, length_penalty=length_penalty, suppress_tokens=suppress_tokens,
                                     condition_on_previous_text=condition_on_previous_text, fp16=fp16,
                                     compression_ratio_threshold=compression_ratio_threshold, logprob_threshold=logprob_threshold, no_speech_threshold=no_speech_threshold, 
                                     word_timestamps=word_timestamps, prepend_punctuations=prepend_punctuations, append_punctuations=append_punctuations, highlight_words=highlight_words,
                                     progress=progress)

    # Perform diarization given a specific input audio file and whisper file
    def perform_extra(self, languageName, urlData, singleFile, whisper_file: str, 
                      highlight_words: bool = False,
                      diarization: bool = False, diarization_speakers: int = None, diarization_min_speakers = 1, diarization_max_speakers = 5, progress=gr.Progress()):
    
        if whisper_file is None:
            raise ValueError("whisper_file is required")

         # Set diarization
        if diarization:
            self.set_diarization(auth_token=self.app_config.auth_token, num_speakers=diarization_speakers if diarization_speakers > 0 else None, 
                                min_speakers=diarization_min_speakers, max_speakers=diarization_max_speakers)
        else:
            self.unset_diarization()
        
        def custom_transcribe_file(source: AudioSource):
            result = load_transcript(whisper_file.name)
            
            # Set language if not set
            if not "language" in result:
                result["language"] = languageName

            # Mark speakers
            result = self._handle_diarization(source.source_path, result)
            return result
        
        multipleFiles = [singleFile] if singleFile else None
        
        # Will return download, text, vtt
        return self.transcribe_webui("base", "", urlData, multipleFiles, None, None, None, 
                                       progress=progress,highlight_words=highlight_words,
                                       override_transcribe_file=custom_transcribe_file, override_max_sources=1)

    def transcribe_webui(self, modelName, languageName, urlData, multipleFiles, microphoneData, task, 
                         vadOptions: VadOptions, progress: gr.Progress = None, highlight_words: bool = False, 
                         override_transcribe_file: Callable[[AudioSource], dict] = None, override_max_sources = None,
                         **decodeOptions: dict):
        try:
            sources = self.__get_source(urlData, multipleFiles, microphoneData)

            if override_max_sources is not None and len(sources) > override_max_sources:
                raise ValueError("Maximum number of sources is " + str(override_max_sources) + ", but " + str(len(sources)) + " were provided")

            try:
                selectedLanguage = languageName.lower() if languageName is not None else None
                selectedModel = modelName if modelName is not None else "base"

                if override_transcribe_file is None:
                    model = create_whisper_container(whisper_implementation=self.app_config.whisper_implementation, 
                                                    model_name=selectedModel, compute_type=self.app_config.compute_type, 
                                                    cache=self.model_cache, models=self.app_config.models)
                else:
                    model = None

                # Result
                download = []
                zip_file_lookup = {}
                text = ""
                vtt = ""

                # Write result
                downloadDirectory = tempfile.mkdtemp()
                source_index = 0

                outputDirectory = self.output_dir if self.output_dir is not None else downloadDirectory

                # Progress
                total_duration = sum([source.get_audio_duration() for source in sources])
                current_progress = 0

                # A listener that will report progress to Gradio
                root_progress_listener = self._create_progress_listener(progress)

                # Execute whisper
                for source in sources:
                    source_prefix = ""
                    source_audio_duration = source.get_audio_duration()

                    if (len(sources) > 1):
                        # Prefix (minimum 2 digits)
                        source_index += 1
                        source_prefix = str(source_index).zfill(2) + "_"
                        print("Transcribing ", source.source_path)

                    scaled_progress_listener = SubTaskProgressListener(root_progress_listener, 
                                                   base_task_total=total_duration,
                                                   sub_task_start=current_progress,
                                                   sub_task_total=source_audio_duration)

                    # Transcribe using the override function if specified
                    if override_transcribe_file is None:
                        result = self.transcribe_file(model, source.source_path, selectedLanguage, task, vadOptions, scaled_progress_listener, **decodeOptions)
                    else:
                        result = override_transcribe_file(source)

                    filePrefix = slugify(source_prefix + source.get_short_name(), allow_unicode=True)

                    # Update progress
                    current_progress += source_audio_duration

                    source_download, source_text, source_vtt = self.write_result(result, filePrefix, outputDirectory, highlight_words)

                    if len(sources) > 1:
                        # Add new line separators
                        if (len(source_text) > 0):
                            source_text += os.linesep + os.linesep
                        if (len(source_vtt) > 0):
                            source_vtt += os.linesep + os.linesep

                        # Append file name to source text too
                        source_text = source.get_full_name() + ":" + os.linesep + source_text
                        source_vtt = source.get_full_name() + ":" + os.linesep + source_vtt

                    # Add to result
                    download.extend(source_download)
                    text += source_text
                    vtt += source_vtt

                    if (len(sources) > 1):
                        # Zip files support at least 260 characters, but we'll play it safe and use 200
                        zipFilePrefix = slugify(source_prefix + source.get_short_name(max_length=200), allow_unicode=True)

                        # File names in ZIP file can be longer
                        for source_download_file in source_download:
                            # Get file postfix (after last -)
                            filePostfix = os.path.basename(source_download_file).split("-")[-1]
                            zip_file_name = zipFilePrefix + "-" + filePostfix
                            zip_file_lookup[source_download_file] = zip_file_name

                # Create zip file from all sources
                if len(sources) > 1:
                    downloadAllPath = os.path.join(downloadDirectory, "All_Output-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip")

                    with zipfile.ZipFile(downloadAllPath, 'w', zipfile.ZIP_DEFLATED) as zip:
                        for download_file in download:
                            # Get file name from lookup
                            zip_file_name = zip_file_lookup.get(download_file, os.path.basename(download_file))
                            zip.write(download_file, arcname=zip_file_name)

                    download.insert(0, downloadAllPath)

                return download, text, vtt

            finally:
                # Cleanup source
                if self.deleteUploadedFiles:
                    for source in sources:
                        print("Deleting source file " + source.source_path)

                        try:
                            os.remove(source.source_path)
                        except Exception as e:
                            # Ignore error - it's just a cleanup
                            print("Error deleting source file " + source.source_path + ": " + str(e))
        
        except ExceededMaximumDuration as e:
            return [], ("[ERROR]: Maximum remote video length is " + str(e.maxDuration) + "s, file was " + str(e.videoDuration) + "s"), "[ERROR]"

    def transcribe_file(self, model: AbstractWhisperContainer, audio_path: str, language: str, task: str = None, 
                        vadOptions: VadOptions = VadOptions(), 
                        progressListener: ProgressListener = None, **decodeOptions: dict):
        
        initial_prompt = decodeOptions.pop('initial_prompt', None)

        if progressListener is None:
            # Default progress listener
            progressListener = ProgressListener()

        if ('task' in decodeOptions):
            task = decodeOptions.pop('task')

        initial_prompt_mode = vadOptions.vadInitialPromptMode

        # Set default initial prompt mode
        if (initial_prompt_mode is None):
            initial_prompt_mode = VadInitialPromptMode.PREPREND_FIRST_SEGMENT

        if (initial_prompt_mode == VadInitialPromptMode.PREPEND_ALL_SEGMENTS or 
            initial_prompt_mode == VadInitialPromptMode.PREPREND_FIRST_SEGMENT):
            # Prepend initial prompt
            prompt_strategy = PrependPromptStrategy(initial_prompt, initial_prompt_mode)
        elif (vadOptions.vadInitialPromptMode == VadInitialPromptMode.JSON_PROMPT_MODE):
            # Use a JSON format to specify the prompt for each segment
            prompt_strategy = JsonPromptStrategy(initial_prompt)
        else:
            raise ValueError("Invalid vadInitialPromptMode: " + initial_prompt_mode)

        # Callable for processing an audio file
        whisperCallable = model.create_callback(language, task, prompt_strategy=prompt_strategy, **decodeOptions)

        # The results
        if (vadOptions.vad == 'silero-vad'):
            # Silero VAD where non-speech gaps are transcribed
            process_gaps = self._create_silero_config(NonSpeechStrategy.CREATE_SEGMENT, vadOptions)
            result = self.process_vad(audio_path, whisperCallable, self.vad_model, process_gaps, progressListener=progressListener)
        elif (vadOptions.vad == 'silero-vad-skip-gaps'):
            # Silero VAD where non-speech gaps are simply ignored
            skip_gaps = self._create_silero_config(NonSpeechStrategy.SKIP, vadOptions)
            result = self.process_vad(audio_path, whisperCallable, self.vad_model, skip_gaps, progressListener=progressListener)
        elif (vadOptions.vad == 'silero-vad-expand-into-gaps'):
            # Use Silero VAD where speech-segments are expanded into non-speech gaps
            expand_gaps = self._create_silero_config(NonSpeechStrategy.EXPAND_SEGMENT, vadOptions)
            result = self.process_vad(audio_path, whisperCallable, self.vad_model, expand_gaps, progressListener=progressListener)
        elif (vadOptions.vad == 'periodic-vad'):
            # Very simple VAD - mark every 5 minutes as speech. This makes it less likely that Whisper enters an infinite loop, but
            # it may create a break in the middle of a sentence, causing some artifacts.
            periodic_vad = VadPeriodicTranscription()
            period_config = PeriodicTranscriptionConfig(periodic_duration=vadOptions.vadMaxMergeSize, max_prompt_window=vadOptions.vadPromptWindow)
            result = self.process_vad(audio_path, whisperCallable, periodic_vad, period_config, progressListener=progressListener)

        else:
            if (self._has_parallel_devices()):
                # Use a simple period transcription instead, as we need to use the parallel context
                periodic_vad = VadPeriodicTranscription()
                period_config = PeriodicTranscriptionConfig(periodic_duration=math.inf, max_prompt_window=1)

                result = self.process_vad(audio_path, whisperCallable, periodic_vad, period_config, progressListener=progressListener)
            else:
                # Default VAD
                result = whisperCallable.invoke(audio_path, 0, None, None, progress_listener=progressListener)
        
        # Diarization
        result = self._handle_diarization(audio_path, result)
        return result

    def _handle_diarization(self, audio_path: str, input: dict):
        if self.diarization and self.diarization_kwargs:
            print("Diarising ", audio_path)
            diarization_result = list(self.diarization.run(audio_path, **self.diarization_kwargs))

            # Print result
            print("Diarisation result: ")
            for entry in diarization_result:
                print(f"  start={entry.start:.1f}s stop={entry.end:.1f}s speaker_{entry.speaker}")

            # Add speakers to result
            input = self.diarization.mark_speakers(diarization_result, input)

        return input

    def _create_progress_listener(self, progress: gr.Progress):
        if (progress is None):
            # Dummy progress listener
            return ProgressListener()
        
        class ForwardingProgressListener(ProgressListener):
            def __init__(self, progress: gr.Progress):
                self.progress = progress

            def on_progress(self, current: Union[int, float], total: Union[int, float]):
                # From 0 to 1
                self.progress(current / total)

            def on_finished(self):
                self.progress(1)

        return ForwardingProgressListener(progress)

    def process_vad(self, audio_path, whisperCallable, vadModel: AbstractTranscription, vadConfig: TranscriptionConfig, 
                    progressListener: ProgressListener = None):
        if (not self._has_parallel_devices()):
            # No parallel devices, so just run the VAD and Whisper in sequence
            return vadModel.transcribe(audio_path, whisperCallable, vadConfig, progressListener=progressListener)

        gpu_devices = self.parallel_device_list

        if (gpu_devices is None or len(gpu_devices) == 0):
            # No GPU devices specified, pass the current environment variable to the first GPU process. This may be NULL.
            gpu_devices = [os.environ.get("CUDA_VISIBLE_DEVICES", None)]

        # Create parallel context if needed
        if (self.gpu_parallel_context is None):
            # Create a context wih processes and automatically clear the pool after 1 hour of inactivity
            self.gpu_parallel_context = ParallelContext(num_processes=len(gpu_devices), auto_cleanup_timeout_seconds=self.vad_process_timeout)
        # We also need a CPU context for the VAD
        if (self.cpu_parallel_context is None):
            self.cpu_parallel_context = ParallelContext(num_processes=self.vad_cpu_cores, auto_cleanup_timeout_seconds=self.vad_process_timeout)

        parallel_vad = ParallelTranscription()
        return parallel_vad.transcribe_parallel(transcription=vadModel, audio=audio_path, whisperCallable=whisperCallable,  
                                                config=vadConfig, cpu_device_count=self.vad_cpu_cores, gpu_devices=gpu_devices, 
                                                cpu_parallel_context=self.cpu_parallel_context, gpu_parallel_context=self.gpu_parallel_context, 
                                                progress_listener=progressListener) 

    def _has_parallel_devices(self):
        return (self.parallel_device_list is not None and len(self.parallel_device_list) > 0) or self.vad_cpu_cores > 1

    def _concat_prompt(self, prompt1, prompt2):
        if (prompt1 is None):
            return prompt2
        elif (prompt2 is None):
            return prompt1
        else:
            return prompt1 + " " + prompt2

    def _create_silero_config(self, non_speech_strategy: NonSpeechStrategy, vadOptions: VadOptions):
        # Use Silero VAD 
        if (self.vad_model is None):
            self.vad_model = VadSileroTranscription()

        config = TranscriptionConfig(non_speech_strategy = non_speech_strategy, 
                max_silent_period=vadOptions.vadMergeWindow, max_merge_size=vadOptions.vadMaxMergeSize, 
                segment_padding_left=vadOptions.vadPadding, segment_padding_right=vadOptions.vadPadding, 
                max_prompt_window=vadOptions.vadPromptWindow)

        return config

    def write_result(self, result: dict, source_name: str, output_dir: str, highlight_words: bool = False):
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        text = result["text"]
        language = result["language"] if "language" in result else None
        languageMaxLineWidth = self.__get_max_line_width(language)

        # We always create the JSON file for debugging purposes
        json_result = json.dumps(result, indent=4, ensure_ascii=False)
        json_file = self.__create_file(json_result, output_dir, source_name + "-result.json")
        print("Created JSON file " + json_file)

        print("Max line width " + str(languageMaxLineWidth))
        vtt = self.__get_subs(result["segments"], "vtt", languageMaxLineWidth, highlight_words=highlight_words)
        srt = self.__get_subs(result["segments"], "srt", languageMaxLineWidth, highlight_words=highlight_words)
        html = self.__get_subs(result["segments"], "html", languageMaxLineWidth, highlight_words=highlight_words)

        output_files = []
        output_files.append(self.__create_file(srt, output_dir, source_name + "-subs.srt"));
        output_files.append(self.__create_file(vtt, output_dir, source_name + "-subs.vtt"));
        output_files.append(self.__create_file(text, output_dir, source_name + "-transcript.txt"));
        output_files.append(self.__create_file(html, output_dir, source_name + "-transcript.html"));
        output_files.append(json_file)

        return output_files, text, vtt

    def clear_cache(self):
        self.model_cache.clear()
        self.vad_model = None

    def __get_source(self, urlData, multipleFiles, microphoneData):
        return get_audio_source_collection(urlData, multipleFiles, microphoneData, self.inputAudioMaxDuration)

    def __get_max_line_width(self, language: str) -> int:
        if (language and language.lower() in ["japanese", "ja", "chinese", "zh"]):
            # Chinese characters and kana are wider, so limit line length to 40 characters
            return 40
        else:
            # TODO: Add more languages
            # 80 latin characters should fit on a 1080p/720p screen
            return 80

    def __get_subs(self, segments: Iterator[dict], format: str, maxLineWidth: int, highlight_words: bool = False) -> str:
        segmentStream = StringIO()

        if format == 'vtt':
            write_vtt(segments, file=segmentStream, maxLineWidth=maxLineWidth, highlight_words=highlight_words)
        elif format == 'srt':
            write_srt(segments, file=segmentStream, maxLineWidth=maxLineWidth, highlight_words=highlight_words)
        elif format == 'html':
            write_html(segments, file=segmentStream, maxLineWidth=maxLineWidth, highlight_words=highlight_words)
        else:
            raise Exception("Unknown format " + format)

        segmentStream.seek(0)
        return segmentStream.read()

    def __create_file(self, text: str, directory: str, fileName: str) -> str:
        # Write the text to a file
        with open(os.path.join(directory, fileName), 'w+', encoding="utf-8") as file:
            file.write(text)

        return file.name

    def close(self):
        print("Closing parallel contexts")
        self.clear_cache()

        if (self.gpu_parallel_context is not None):
            self.gpu_parallel_context.close()
        if (self.cpu_parallel_context is not None):
            self.cpu_parallel_context.close()

        # Cleanup diarization
        if (self.diarization is not None):
            self.diarization.cleanup()
            self.diarization = None

def create_ui(app_config: ApplicationConfig):
    ui = WhisperTranscriber(app_config.input_audio_max_duration, app_config.vad_process_timeout, app_config.vad_cpu_cores, 
                            app_config.delete_uploaded_files, app_config.output_dir, app_config)

    # Specify a list of devices to use for parallel processing
    ui.set_parallel_devices(app_config.vad_parallel_devices)
    ui.set_auto_parallel(app_config.auto_parallel)

    is_whisper = False

    if app_config.whisper_implementation == "whisper":
        implementation_name = "Whisper"
        is_whisper = True
    elif app_config.whisper_implementation in ["faster-whisper", "faster_whisper"]:
        implementation_name = "Faster Whisper"
    else:
        # Try to convert from camel-case to title-case
        implementation_name = app_config.whisper_implementation.title().replace("_", " ").replace("-", " ")

    ui_description = implementation_name + " is a general-purpose speech recognition model. It is trained on a large dataset of diverse " 
    ui_description += " audio and is also a multi-task model that can perform multilingual speech recognition "
    ui_description += " as well as speech translation and language identification. "

    ui_description += "\n\n\n\nFor longer audio files (>10 minutes) not in English, it is recommended that you select Silero VAD (Voice Activity Detector) in the VAD option."

    # Recommend faster-whisper
    if is_whisper:
        ui_description += "\n\n\n\nFor faster inference on GPU, try [faster-whisper](https://huggingface.co/spaces/aadnk/faster-whisper-webui)."

    if app_config.input_audio_max_duration > 0:
        ui_description += "\n\n" + "Max audio file length: " + str(app_config.input_audio_max_duration) + " s"

    #ui_article = "Read the [documentation here](https://gitlab.com/aadnk/whisper-webui/-/blob/main/docs/options.md)."
    ui_article = "Read the [documentation here](https://rtis.cspages.otago.ac.nz/research-computing/misc/stt.html)."

    whisper_models = app_config.get_model_names()

    common_inputs = lambda : [
        gr.Dropdown(choices=whisper_models, value=app_config.default_model_name, label="Model"),
        gr.Dropdown(choices=sorted(get_language_names()), label="Language", value=app_config.language),
        gr.Text(label="URL (YouTube, etc.)"),
        gr.File(label="Upload Files", file_count="multiple"),
        gr.Audio(sources=["microphone"], type="filepath", label="Microphone Input"),
        gr.Dropdown(choices=["transcribe", "translate"], label="Task", value=app_config.task),
    ]

    common_vad_inputs = lambda : [
        gr.Dropdown(choices=["none", "silero-vad", "silero-vad-skip-gaps", "silero-vad-expand-into-gaps", "periodic-vad"], value=app_config.default_vad, label="VAD"),
        gr.Number(label="VAD - Merge Window (s)", precision=0, value=app_config.vad_merge_window),
        gr.Number(label="VAD - Max Merge Size (s)", precision=0, value=app_config.vad_max_merge_size),
    ]
    
    common_word_timestamps_inputs = lambda : [
        gr.Checkbox(label="Word Timestamps", value=app_config.word_timestamps),
        gr.Checkbox(label="Word Timestamps - Highlight Words", value=app_config.highlight_words),
    ]

    has_diarization_libs = Diarization.has_libraries()

    if not has_diarization_libs:
        print("Diarisation libraries not found - disabling diarization")
        app_config.diarization = False

    common_diarization_inputs = lambda : [
        gr.Checkbox(label="Diarisation", value=app_config.diarization, interactive=has_diarization_libs),
        gr.Number(label="Diarisation - Speakers", precision=0, value=None, interactive=has_diarization_libs)
    ]

    is_queue_mode = app_config.queue_concurrency_count is not None and app_config.queue_concurrency_count > 0    

    simple_transcribe = gr.Interface(fn=ui.transcribe_webui_simple_progress if is_queue_mode else ui.transcribe_webui_simple, 
                                     allow_flagging="never",description=ui_description, article=ui_article, inputs=[
        *common_inputs(),
        *common_vad_inputs(),
        *common_word_timestamps_inputs(),
        *common_diarization_inputs(),
    ], outputs=[
        gr.File(label="Download"),
        gr.Text(label="Transcription"), 
        gr.Text(label="Segments")
    ])

    full_description = ui_description + "\n\n\n\n" + "Be careful when changing some of the options in the full interface - this can cause the model to crash."

    full_transcribe = gr.Interface(fn=ui.transcribe_webui_full_progress if is_queue_mode else ui.transcribe_webui_full,
                                   allow_flagging="never",description=full_description, article=ui_article, inputs=[
        *common_inputs(),

        *common_vad_inputs(),
        gr.Number(label="VAD - Padding (s)", precision=None, value=app_config.vad_padding),
        gr.Number(label="VAD - Prompt Window (s)", precision=None, value=app_config.vad_prompt_window),
        gr.Dropdown(choices=VAD_INITIAL_PROMPT_MODE_VALUES, label="VAD - Initial Prompt Mode"),
        
        *common_word_timestamps_inputs(),
        gr.Text(label="Word Timestamps - Prepend Punctuations", value=app_config.prepend_punctuations),
        gr.Text(label="Word Timestamps - Append Punctuations", value=app_config.append_punctuations),

        gr.TextArea(label="Initial Prompt"),
        gr.Number(label="Temperature", value=app_config.temperature),
        gr.Number(label="Best Of - Non-zero temperature", value=app_config.best_of, precision=0),
        gr.Number(label="Beam Size - Zero temperature", value=app_config.beam_size, precision=0),
        gr.Number(label="Patience - Zero temperature", value=app_config.patience),
        gr.Number(label="Length Penalty - Any temperature", value=app_config.length_penalty), 
        gr.Text(label="Suppress Tokens - Comma-separated list of token IDs", value=app_config.suppress_tokens),
        gr.Checkbox(label="Condition on previous text", value=app_config.condition_on_previous_text),
        gr.Checkbox(label="FP16", value=app_config.fp16),
        gr.Number(label="Temperature increment on fallback", value=app_config.temperature_increment_on_fallback),
        gr.Number(label="Compression ratio threshold", value=app_config.compression_ratio_threshold),
        gr.Number(label="Logprob threshold", value=app_config.logprob_threshold),
        gr.Number(label="No speech threshold", value=app_config.no_speech_threshold),

        *common_diarization_inputs(),
        gr.Number(label="Diarisation - Min Speakers", precision=0, value=app_config.diarization_min_speakers, interactive=has_diarization_libs),
        gr.Number(label="Diarisation - Max Speakers", precision=0, value=app_config.diarization_max_speakers, interactive=has_diarization_libs),

    ], outputs=[
        gr.File(label="Download"),
        gr.Text(label="Transcription"), 
        gr.Text(label="Segments")
    ])

    perform_extra_interface = gr.Interface(fn=ui.perform_extra,
                                   allow_flagging="never",description="Perform additional processing on a given JSON or SRT file", article=ui_article, inputs=[
        gr.Dropdown(choices=sorted(get_language_names()), label="Language", value=app_config.language),
        gr.Text(label="URL (YouTube, etc.)"),
        gr.File(label="Upload Audio File", file_count="single"),
        gr.File(label="Upload JSON/SRT File", file_count="single"),
        gr.Checkbox(label="Word Timestamps - Highlight Words", value=app_config.highlight_words),

        *common_diarization_inputs(),
        gr.Number(label="Diarisation - Min Speakers", precision=0, value=app_config.diarization_min_speakers, interactive=has_diarization_libs),
        gr.Number(label="Diarisation - Max Speakers", precision=0, value=app_config.diarization_max_speakers, interactive=has_diarization_libs),

    ], outputs=[
        gr.File(label="Download"),
        gr.Text(label="Transcription"), 
        gr.Text(label="Segments")
    ])

    demo = gr.TabbedInterface([simple_transcribe, full_transcribe, perform_extra_interface], tab_names=["Simple", "Full", "Extra"], title=app_config.title)

    # Queue up the demo
    #if is_queue_mode:
    #    demo.queue(concurrency_count=app_config.queue_concurrency_count)
    #    print("Queue mode enabled (concurrency count: " + str(app_config.queue_concurrency_count) + ")")
    #else:
    #    print("Queue mode disabled - progress bars will not be shown.")
   
    demo.launch(share=app_config.share, server_name=app_config.server_name, server_port=app_config.server_port, max_threads=app_config.queue_concurrency_count, root_path=app_config.root_path)
    
    # Clean up
    ui.close()

if __name__ == '__main__':
    default_app_config = ApplicationConfig.create_default()
    whisper_models = default_app_config.get_model_names()

    # Environment variable overrides
    default_whisper_implementation = os.environ.get("WHISPER_IMPLEMENTATION", default_app_config.whisper_implementation)

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--input_audio_max_duration", type=int, default=default_app_config.input_audio_max_duration, \
                        help="Maximum audio file length in seconds, or -1 for no limit.") # 600
    parser.add_argument("--share", type=bool, default=default_app_config.share, \
                        help="True to share the app on HuggingFace.") # False
    parser.add_argument("--server_name", type=str, default=default_app_config.server_name, \
                        help="The host or IP to bind to. If None, bind to localhost.") # None
    parser.add_argument("--server_port", type=int, default=default_app_config.server_port, \
                        help="The port to bind to.") # 7860
    parser.add_argument("--title", type=str, default=default_app_config.title, \
                        help="Title") # /
    parser.add_argument("--root_path", type=str, default=default_app_config.root_path, \
                        help="Root path.") # /
    parser.add_argument("--queue_concurrency_count", type=int, default=default_app_config.queue_concurrency_count, \
                        help="The number of concurrent requests to process.") # 1
    parser.add_argument("--default_model_name", "--model", type=str, choices=whisper_models, default=default_app_config.default_model_name, \
                        help="The default model name.") # medium
    parser.add_argument("--default_vad", type=str, default=default_app_config.default_vad, \
                        help="The default VAD.") # silero-vad
    parser.add_argument("--vad_initial_prompt_mode", type=str, default=default_app_config.vad_initial_prompt_mode, choices=VAD_INITIAL_PROMPT_MODE_VALUES, \
                        help="Whether or not to prepend the initial prompt to each VAD segment (prepend_all_segments), or just the first segment (prepend_first_segment)") # prepend_first_segment
    parser.add_argument("--vad_parallel_devices", type=str, default=default_app_config.vad_parallel_devices, \
                        help="A commma delimited list of CUDA devices to use for parallel processing. If None, disable parallel processing.") # ""
    parser.add_argument("--vad_cpu_cores", type=int, default=default_app_config.vad_cpu_cores, \
                        help="The number of CPU cores to use for VAD pre-processing.") # 1
    parser.add_argument("--vad_process_timeout", type=float, default=default_app_config.vad_process_timeout, \
                        help="The number of seconds before inactivate processes are terminated. Use 0 to close processes immediately, or None for no timeout.") # 1800
    parser.add_argument("--auto_parallel", type=bool, default=default_app_config.auto_parallel, \
                        help="True to use all available GPUs and CPU cores for processing. Use vad_cpu_cores/vad_parallel_devices to specify the number of CPU cores/GPUs to use.") # False
    parser.add_argument("--output_dir", "-o", type=str, default=default_app_config.output_dir, \
                        help="directory to save the outputs")
    parser.add_argument("--whisper_implementation", type=str, default=default_whisper_implementation, choices=["whisper", "faster-whisper"],\
                        help="the Whisper implementation to use")
    parser.add_argument("--compute_type", type=str, default=default_app_config.compute_type, choices=["default", "auto", "int8", "int8_float16", "int16", "float16", "float32"], \
                        help="the compute type to use for inference")
    parser.add_argument("--threads", type=optional_int, default=0, 
                        help="number of threads used by torch for CPU inference; supercedes MKL_NUM_THREADS/OMP_NUM_THREADS")
    parser.add_argument("--language", type=str, default=default_app_config.language, choices=sorted(get_language_names()), \
                        help="language spoken in the audio, specify None to perform language detection")

    parser.add_argument('--auth_token', type=str, default=default_app_config.auth_token, help='HuggingFace API Token (optional)')
    parser.add_argument("--diarization", type=str2bool, default=default_app_config.diarization, \
                        help="whether to perform speaker diarization")
    parser.add_argument("--diarization_num_speakers", type=int, default=default_app_config.diarization_speakers, help="Number of speakers")
    parser.add_argument("--diarization_min_speakers", type=int, default=default_app_config.diarization_min_speakers, help="Minimum number of speakers")
    parser.add_argument("--diarization_max_speakers", type=int, default=default_app_config.diarization_max_speakers, help="Maximum number of speakers")
    parser.add_argument("--diarization_process_timeout", type=int, default=default_app_config.diarization_process_timeout, \
                        help="Number of seconds before inactivate diarization processes are terminated. Use 0 to close processes immediately, or None for no timeout.")

    args = parser.parse_args().__dict__

    updated_config = default_app_config.update(**args)

    if (threads := args.pop("threads")) > 0:
        torch.set_num_threads(threads)

    print("Using whisper implementation: " + updated_config.whisper_implementation)
    create_ui(app_config=updated_config)
