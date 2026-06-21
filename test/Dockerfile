FROM python:3.10-slim

# System deps for building Python packages with C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone OpenStack Cinder. Pin CINDER_REF for reproducibility;
# override at build time with: docker compose build --build-arg CINDER_REF=stable/2024.1
ARG CINDER_REF=master
RUN git clone --depth 1 --branch ${CINDER_REF} \
    https://github.com/openstack/cinder /cinder

# Install cinder test dependencies.
# We use PYTHONPATH rather than pip install -e . to avoid a setuptools
# compatibility issue (AttributeError: get_requires_for_build_editable).
# The upper-constraints file pins exact compatible package versions — critical
# because newer cryptography drops APIs that older cursive/oslo deps require.
WORKDIR /cinder
# cursive (OpenStack image-signature library) calls ec.SECT571K1() which was
# removed in cryptography 42. Pin it via a local constraint file so pip cannot
# upgrade past that boundary when resolving the full requirements tree.
RUN echo "cryptography<42" > /tmp/pin.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
       -c /tmp/pin.txt \
       stestr \
       -r requirements.txt \
       -r test-requirements.txt

# Put cinder on the path without an editable install
ENV PYTHONPATH=/cinder

# ---- Driver source files ----
# Copied from the repo root (build context = vmstore-cinder-driver/)
COPY api.py nfs.py options.py utils.py __init__.py \
    /cinder/cinder/volume/drivers/vmstore/

# ---- Test files ----
# The vmstore test tree is not in upstream OpenStack cinder; we maintain it
# alongside the driver. test/unit/ holds the authoritative copies.
RUN mkdir -p /cinder/cinder/tests/unit/volume/drivers/vmstore
COPY test/unit/ /cinder/cinder/tests/unit/volume/drivers/vmstore/

# ---- Entrypoint ----
COPY test/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /cinder
ENTRYPOINT ["/entrypoint.sh"]
