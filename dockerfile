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

# cudnn
#WORKDIR /tmp
#RUN wget https://developer.download.nvidia.com/compute/redist/cudnn/v8.8.0/local_installers/12.0/cudnn-local-repo-debian11-8.8.0.121_1.0-1_amd64.deb \
#  && dpkg -i cudnn-local-repo-debian11-8.8.0.121_1.0-1_amd64.deb

#ARG WHISPER_IMPLEMENTATION=whisper
ARG WHISPER_IMPLEMENTATION=faster-whisper
ENV WHISPER_IMPLEMENTATION=${WHISPER_IMPLEMENTATION}

ADD . /opt/whisper-webui/

RUN rm /usr/lib/python3.11/EXTERNALLY-MANAGED

# Latest version of transformers-pytorch-gpu seems to lack tk. 
# Further, pip install fails, so we must upgrade pip first.
RUN  python3 -m pip install --upgrade pip

RUN if [ "${WHISPER_IMPLEMENTATION}" = "whisper" ]; then \
    python3 -m pip install -r /opt/whisper-webui/requirements-whisper.txt; \
  else \
    python3 -m pip install -r /opt/whisper-webui/requirements-fasterWhisper.txt; \
  fi

# Note: Models will be downloaded on demand to the directory /root/.cache/whisper.
# You can also bind this directory in the container to somewhere on the host.

# To be able to see logs in real time
ENV PYTHONUNBUFFERED=1

EXPOSE 7860
WORKDIR /opt/whisper-webui/
#ENTRYPOINT ["python3"]
CMD ["python3", "app.py", "--input_audio_max_duration", "-1", "--server_name", "0.0.0.0", "--auto_parallel", "True"]
