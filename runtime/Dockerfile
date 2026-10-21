ARG WIZ_REGISTRY=registry.os.wiz.io

# Build stage: WizOS full Python image (has apk, shell)
FROM ${WIZ_REGISTRY}/python:3.12 AS build

# Stage runtime C++ libs and Triton's argv-exec C compiler toolchain into /stage
RUN mkdir -p /stage && \
    apk add --root /stage --initdb --no-cache --no-scripts \
        --repositories-file /etc/apk/repositories \
        --keys-dir /etc/apk/keys \
        libstdc++ libgcc gcc binutils glibc-dev && \
    if [ -d /stage/lib ]; then cp -a /stage/lib/. /stage/usr/lib/; fi && \
    if [ -d /stage/usr/lib64 ]; then cp -a /stage/usr/lib64/. /stage/usr/lib/; fi

# Strip glibc/loader families so hardened runtime's glibc is never overwritten
RUN rm -rf \
        /stage/usr/lib/ld-linux* \
        /stage/usr/lib/libc.so* \
        /stage/usr/lib/libm.so* \
        /stage/usr/lib/libpthread.so* \
        /stage/usr/lib/libdl.so* \
        /stage/usr/lib/librt.so* \
        /stage/usr/lib/libutil.so* \
        /stage/usr/lib/libresolv.so* \
        /stage/usr/lib/libnss_* \
        /stage/usr/lib/libanl.so* \
        /stage/usr/lib/libmvec.so* \
        /stage/usr/lib/libthread_db.so* \
        /stage/usr/lib/libBrokenLocale.so* \
        /stage/usr/lib/gconv \
        /stage/usr/lib/locale \
        /stage/usr/lib/apk \
        /stage/lib/apk

# Install torch and torchvision into /opt/venv
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu128 \
        torch==2.11.0+cu128 torchvision==0.26.0+cu128 && \
    find /opt/venv/lib/python3.12/site-packages/nvidia/cudnn/lib \
        -name 'libcudnn_*.so*' ! -name 'libcudnn_graph.so*' -delete && \
    find /opt/venv -name '*.a' -delete && \
    rm -rf /opt/venv/lib/python3.12/site-packages/torch/include \
           /opt/venv/lib/python3.12/site-packages/torch/caffe2

WORKDIR /app
COPY requirements.txt .
COPY --from=compiled_wheels /wheels /wheels
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt && \
    /opt/venv/bin/pip install --no-cache-dir --no-deps /wheels/*.whl && \
    rm -rf /wheels

COPY app.py preload.py fast_path.py request_body.py media.py models.py telemetry.py build_manifest.py models.json manifest.json manifest-flash.json ./

# Pre-compile Triton's CPU-buildable C driver wrapper (CudaUtils) into /opt/triton-cache,
# run check_installation() CPU ABI gate, and precompile all Python bytecode with
# --invalidation-mode unchecked-hash across /app and /opt/venv.
RUN mkdir -p /opt/triton-cache && \
    TRITON_CACHE_DIR=/opt/triton-cache /opt/venv/bin/python -c "from triton.backends.nvidia.driver import CudaUtils; CudaUtils()" && \
    /opt/venv/bin/python -c "import app, preload; from fast_path import check_installation; check_installation()" && \
    /opt/venv/bin/python -m compileall -q -j 0 --invalidation-mode unchecked-hash /app /opt/venv

# Runtime stage: WizOS hardened (no shell, no apk, distroless)
FROM ${WIZ_REGISTRY}/python:3.12-hardened

# Copy staged C++ libs and compiler toolchain into /usr/
COPY --from=build /stage/usr/ /usr/
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/triton-cache /opt/triton-cache
COPY --from=build /app /app

WORKDIR /app

# Generate immutable /app/build-manifest.json (0444) as root before dropping to non-root USER 65532:65532
ARG BUILD_ID=development
ARG SOURCE_REVISION=unknown
USER 0:0
RUN ["/opt/venv/bin/python", "/app/build_manifest.py"]

ENV HOME=/tmp \
    TRITON_CACHE_DIR=/tmp/triton-cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PATH="/opt/venv/bin:$PATH"

USER 65532:65532

EXPOSE 8080

ENTRYPOINT []
CMD ["/opt/venv/bin/python", "app.py"]
