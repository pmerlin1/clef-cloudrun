FROM python:3.12-slim-trixie
WORKDIR /app
# Triton needs a C compiler to build its small driver launcher at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
# Native SDPA/FLA/causal-conv1d do not use cuDNN engine plugins;
# fast_path.py explicitly disables cuDNN dispatch.
# Delete in the installation layer so unused plugins never enter image layers.
RUN python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu128 \
    torch==2.11.0+cu128 torchvision==0.26.0+cu128 \
    && find /usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib \
    -name 'libcudnn_*.so*' ! -name 'libcudnn_graph.so*' -delete
COPY requirements.txt .
COPY --from=compiled_wheels /wheels /wheels
RUN python -m pip install --break-system-packages --no-cache-dir -r requirements.txt \
    && python -m pip install --break-system-packages --no-cache-dir --no-deps /wheels/*.whl \
    && rm -rf /wheels
COPY app.py preload.py fast_path.py request_body.py media.py models.py models.json manifest.json manifest-flash.json ./
# CPU-only build gate: importing the CUDA extension catches ABI/linker failures.
RUN python -c "import app, preload; from fast_path import check_installation; check_installation()"
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONUNBUFFERED=1
CMD ["python", "app.py"]
