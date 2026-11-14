FROM python@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=65534:65534 . .
USER 65534:65534
CMD ["python", "-m", "curve_comparison", "demo"]
