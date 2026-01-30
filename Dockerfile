FROM python:alpine AS downloader

RUN apk add --no-cache wget unzip

WORKDIR /src

# download models
RUN wget https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip && \
    unzip realesrgan-ncnn-vulkan-20220424-ubuntu.zip && rm *.mp4 *.jpg realesrgan*

ARG TARGETARCH

# Download realesrgan-vulkan-ncnn executable binaries
RUN if [ "$TARGETARCH" == "amd64" ]; then \
        wget https://github.com/Taprium/Real-ESRGAN-ncnn-vulkan-alpine/releases/download/v0.0.1/realesrgan-ncnn-vulkan-alpine-x64 -O realesrgan-ncnn-vulkan; \
    elif [ "$TARGETARCH" == "arm64" ]; then \
        wget https://github.com/Taprium/Real-ESRGAN-ncnn-vulkan-alpine/releases/download/v0.0.1/realesrgan-ncnn-vulkan-alpine-arm64 -O realesrgan-ncnn-vulkan; \
    elif [ "$TARGETARCH" == "arm" ]; then \
        wget https://github.com/Taprium/Real-ESRGAN-ncnn-vulkan-alpine/releases/download/v0.0.1/realesrgan-ncnn-vulkan-alpine-arm32 -O realesrgan-ncnn-vulkan; \
    fi

FROM python:alpine

WORKDIR /app

RUN apk update && \
    apk add --no-cache vulkan-loader libgomp libgcc && \
    apk search -eq 'mesa-vulkan-*' | grep -v 'layers' | xargs apk add --no-cache &&\
    rm -rf /var/cache/apk/*

RUN pip install pocketbase filelock requests schedule

COPY crontab.txt *.py *.sh ./
COPY --from=downloader /src/realesrgan-ncnn-vulkan ./
COPY --from=downloader /src/models/* ./models/

RUN rm crontab.txt && chmod +x /app/realesrgan-ncnn-vulkan

ENTRYPOINT [ "python", "upscale-runner.py"]
