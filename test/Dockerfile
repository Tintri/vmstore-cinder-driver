# Python 3.12 satisfies both stable/2025.1 (3.9 to 3.12) and current Cinder
# master (3.11 or later).
FROM python:3.12-slim

# System deps for building Python packages with C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    libc6-dev \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone OpenStack Cinder. Defaults to the release Platform9 PCD ships;
# override at build time with: docker compose build --build-arg CINDER_REF=master
ARG CINDER_REF=stable/2025.1
RUN git clone --depth 1 --branch ${CINDER_REF} \
    https://github.com/openstack/cinder /cinder

# Install cinder test dependencies, pinned with the OpenStack upper-constraints
# that match CINDER_REF (stable/2025.1 -> .../upper/2025.1, master -> .../upper/master).
# Without them pip resolves the newest libraries, which break older Cinder
# branches (for example oslo.utils dropping secretutils.md5).
# Set UPPER_CONSTRAINTS_URL to use a different constraints file.
# setuptools is listed explicitly because Python 3.12 images no longer ship it.
# Stable branches still import pkg_resources (removed in newer setuptools), so
# they get setuptools<80; master gets the current release.
# We use PYTHONPATH rather than pip install -e . to avoid a setuptools
# compatibility issue (AttributeError: get_requires_for_build_editable).
WORKDIR /cinder
ARG UPPER_CONSTRAINTS_URL=
RUN UC="${UPPER_CONSTRAINTS_URL:-https://releases.openstack.org/constraints/upper/${CINDER_REF#stable/}}" \
    && case "${CINDER_REF}" in master) ST="setuptools" ;; *) ST="setuptools<80" ;; esac \
    && echo "Using upper-constraints: ${UC}, ${ST}" \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
       -c "${UC}" \
       "${ST}" \
       stestr \
       -r requirements.txt \
       -r test-requirements.txt

# Register cinder's stevedore entry points (scheduler filters, volume drivers,
# etc.) without doing a full pip install -e . which fails on older setuptools.
# setup.py egg_info writes cinder.egg-info/entry_points.txt which stevedore
# reads at runtime — making get_all_classes() return the filter classes.
RUN python setup.py egg_info 2>/dev/null || true

# Put cinder on the path without an editable install
ENV PYTHONPATH=/cinder

# ---- Driver source files ----
# Copied from the repo root (build context = vmstore-cinder-driver/)
COPY api.py nfs.py options.py utils.py __init__.py \
    /cinder/cinder/volume/drivers/vmstore/

# ---- Test files ----
# The VMstore tests live in this repo and are copied into the cloned Cinder
# tree so we can use Cinder's native stestr-based unit and functional harness.
RUN mkdir -p /cinder/cinder/tests/unit/volume/drivers/vmstore
COPY test/unit/ /cinder/cinder/tests/unit/volume/drivers/vmstore/

COPY test/functional/test_vmstore_driver.py \
    /cinder/cinder/tests/functional/test_vmstore_driver.py

# ---- Entrypoint ----
COPY test/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /cinder
ENTRYPOINT ["/entrypoint.sh"]
