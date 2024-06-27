# docker build -t whisper-webui --build-arg WHISPER_IMPLEMENTATION=whisper .



#FROM huggingface/transformers-pytorch-gpu
FROM debian:bookworm-slim

LABEL maintainer="wimg"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y \
      curl \
      wget \
      ffmpeg \
      python3 \
      python3-pip \
      python3-tk \
    && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

#ARG WHISPER_IMPLEMENTATION=whisper
ARG WHISPER_IMPLEMENTATION=faster-whisper
ENV WHISPER_IMPLEMENTATION=${WHISPER_IMPLEMENTATION}

# stop nagging, Debian
RUN rm /usr/lib/python3.11/EXTERNALLY-MANAGED

# Latest version of transformers-pytorch-gpu seems to lack tk. 
# Further, pip install fails, so we must upgrade pip first.
RUN  python3 -m pip install --upgrade pip

# copy & install requirements first; keeps things cached and makes rebuilding much faster whenever anything in app changes
COPY requirements-whisper.txt requirements-fasterWhisper.txt /tmp/

RUN if [ "${WHISPER_IMPLEMENTATION}" = "whisper" ]; then \
    python3 -m pip install -r /tmp/requirements-whisper.txt; \
  else \
    python3 -m pip install -r /tmp/requirements-fasterWhisper.txt; \
  fi
RUN rm /tmp/*

ADD . /opt/whisper-webui/

# Note: Models will be downloaded on demand to the directory /root/.cache/whisper.
# You can also bind this directory in the container to somewhere on the host.

# To be able to see logs in real time
ENV PYTHONUNBUFFERED=1

EXPOSE 7860
WORKDIR /opt/whisper-webui/
#ENTRYPOINT ["python3"]
CMD ["python3", "app.py", "--input_audio_max_duration", "-1", "--server_name", "0.0.0.0", "--auto_parallel", "True"]
