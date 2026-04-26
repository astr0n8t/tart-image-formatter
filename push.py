#!/usr/bin/env python3
"""
push.py: Push a raw disk image and config.json to an OCI registry in a format
that is compatible with Cirrus Labs' tart macOS virtualization tool.

The image can then be pulled and run with:
    tart pull <reference>
    tart run <name>

References:
  https://github.com/cirruslabs/tart/blob/main/Sources/tart/OCI/Layerizer/DiskV2.swift
  https://github.com/cirruslabs/tart/blob/main/Sources/tart/OCI/Manifest.swift
  https://github.com/cirruslabs/tart/blob/main/Sources/tart/VMDirectory+OCI.swift
"""

import argparse
import hashlib
import json
import os
import re
import struct
import sys
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, urljoin

import lz4.block
import requests

# ---------------------------------------------------------------------------
# OCI / tart media types
# ---------------------------------------------------------------------------
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
TART_CONFIG_MEDIA_TYPE = "application/vnd.cirruslabs.tart.config.v1"
TART_DISK_V2_MEDIA_TYPE = "application/vnd.cirruslabs.tart.disk.v2"
TART_NVRAM_MEDIA_TYPE = "application/vnd.cirruslabs.tart.nvram.v1"

# ---------------------------------------------------------------------------
# tart OCI annotation / label keys
# ---------------------------------------------------------------------------
UNCOMPRESSED_DISK_SIZE_ANNOTATION = "org.cirruslabs.tart.uncompressed-disk-size"
UPLOAD_TIME_ANNOTATION = "org.cirruslabs.tart.upload-time"
UNCOMPRESSED_SIZE_ANNOTATION = "org.cirruslabs.tart.uncompressed-size"
UNCOMPRESSED_CONTENT_DIGEST_ANNOTATION = "org.cirruslabs.tart.uncompressed-content-digest"
DISK_FORMAT_LABEL = "org.cirruslabs.tart.disk.format"

# ---------------------------------------------------------------------------
# Disk chunking — matches DiskV2.layerLimitBytes
# ---------------------------------------------------------------------------
LAYER_LIMIT_BYTES = 512 * 1024 * 1024  # 512 MiB per OCI layer

# ---------------------------------------------------------------------------
# Apple LZ4 framing format constants
#
# Apple's Compression framework uses a custom stream framing format that
# wraps raw LZ4 blocks.  The format is documented in the LZFSE open-source
# library (https://github.com/lzfse/lzfse) inside lzfse_internal.h.
#
# Each block begins with a 4-byte magic number (little-endian uint32):
#   LZ4_BLOCK_MAGIC       = 0x184D2204  — compressed LZ4 block
#   LZFSE_UNCOMPRESSED_MAGIC = 0x2D787662  — uncompressed ("bvx-")
#   LZFSE_ENDOFSTREAM_MAGIC  = 0x24787662  — end of stream ("bvx$")
#
# A compressed block header (lz4_block_header, 12 bytes total):
#   uint32_t magic            (LZ4_BLOCK_MAGIC)
#   uint32_t n_raw_bytes      (uncompressed size, LE)
#   uint32_t n_payload_bytes  (compressed size, LE)
# followed immediately by n_payload_bytes of raw LZ4 block data.
#
# An uncompressed block header (8 bytes):
#   uint32_t magic        (LZFSE_UNCOMPRESSED_MAGIC)
#   uint32_t n_raw_bytes  (data size, LE)
# followed immediately by n_raw_bytes of raw data.
#
# The stream ends with a single 4-byte LZFSE_ENDOFSTREAM_MAGIC word.
# ---------------------------------------------------------------------------
LZ4_BLOCK_MAGIC = 0x184D2204
LZFSE_UNCOMPRESSED_MAGIC = 0x2D787662
LZFSE_ENDOFSTREAM_MAGIC = 0x24787662


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha256_digest(data: bytes) -> str:
    """Return a ``sha256:``-prefixed hex digest string."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def apple_lz4_compress(data: bytes) -> bytes:
    """Compress *data* using Apple's LZ4 stream framing format.

    This produces output that can be decompressed by Apple's Compression
    framework (``OutputFilter(.decompress, using: .lz4)``), which is what
    tart uses when pulling disk layers.

    The raw LZ4 block compression is delegated to the ``lz4`` Python library
    (``lz4.block.compress``), and the result is wrapped in Apple's framing.
    If LZ4 compression would increase the data size, an uncompressed block is
    emitted instead.
    """
    compressed = lz4.block.compress(data, store_size=False)

    if len(compressed) < len(data):
        # LZ4-compressed block
        header = struct.pack("<III", LZ4_BLOCK_MAGIC, len(data), len(compressed))
        payload = header + compressed
    else:
        # Uncompressed block — LZ4 did not help
        header = struct.pack("<II", LZFSE_UNCOMPRESSED_MAGIC, len(data))
        payload = header + data

    # Append end-of-stream marker
    payload += struct.pack("<I", LZFSE_ENDOFSTREAM_MAGIC)
    return payload


def parse_reference(reference: str) -> tuple:
    """Parse an OCI image reference into *(host, namespace, tag)*.

    Supports the following forms::

        ghcr.io/username/repo:tag
        registry.example.com/namespace/image:latest
        myimage:v1.0  (assumes Docker Hub)
    """
    # Separate tag suffix
    # The tag is the part after the last colon that appears after the last slash
    last_slash_idx = reference.rfind("/")
    after_last_slash = reference[last_slash_idx + 1:]
    if ":" in after_last_slash:
        colon_idx = reference.rfind(":")
        tag = reference[colon_idx + 1:]
        path = reference[:colon_idx]
    else:
        tag = "latest"
        path = reference

    # Separate host from namespace
    # A host contains a dot, a colon (port), or is "localhost"
    parts = path.split("/", 1)
    if len(parts) == 1:
        host = "registry-1.docker.io"
        namespace = f"library/{parts[0]}"
    else:
        potential_host = parts[0]
        if "." in potential_host or ":" in potential_host or potential_host == "localhost":
            host = potential_host
            namespace = parts[1]
        else:
            # No recognisable host — treat as Docker Hub user/repo
            host = "registry-1.docker.io"
            namespace = path

    return host, namespace, tag


def _os_for_oci(tart_os: str) -> str:
    """Map tart OS names to OCI ``os`` field values."""
    mapping = {"macos": "darwin", "darwin": "darwin", "linux": "linux"}
    return mapping.get(tart_os.lower(), tart_os.lower())


# ---------------------------------------------------------------------------
# OCI registry client
# ---------------------------------------------------------------------------

class OCIRegistry:
    """Minimal OCI Distribution Spec client with Bearer / Basic auth support."""

    def __init__(
        self,
        host: str,
        namespace: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        insecure: bool = False,
    ) -> None:
        proto = "http" if insecure else "https"
        self._base_url = f"{proto}://{host}"
        self._namespace = namespace
        self._username = username
        self._password = password
        self._token: Optional[str] = None
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def _fetch_token(self, www_authenticate: str) -> None:
        """Parse *www_authenticate* and obtain a bearer token."""
        parts = www_authenticate.split(" ", 1)
        scheme = parts[0].strip().lower()

        if scheme == "basic":
            # Basic auth credentials are sent directly — no token needed.
            self._token = None
            return

        if scheme != "bearer":
            raise RuntimeError(
                f"Unsupported WWW-Authenticate scheme: {scheme!r}.  "
                "Only 'Bearer' and 'Basic' are supported."
            )

        # Parse key="value" pairs from the WWW-Authenticate header.
        bearer_params: dict = {}
        if len(parts) > 1:
            for m in re.finditer(r'(\w+)="([^"]*)"', parts[1]):
                bearer_params[m.group(1)] = m.group(2)

        realm = bearer_params.get("realm", "")
        if not realm:
            raise RuntimeError("Bearer WWW-Authenticate header is missing 'realm'")

        # Request a token from the auth endpoint.
        token_params = {
            k: bearer_params[k] for k in ("scope", "service") if k in bearer_params
        }
        auth = (self._username, self._password) if self._username else None
        resp = self._session.get(realm, params=token_params, auth=auth, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        self._token = data.get("token") or data.get("access_token")
        if not self._token:
            raise RuntimeError(
                "Token endpoint response contained neither 'token' nor 'access_token'"
            )

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Make an authenticated registry request, retrying once after a 401."""
        url = f"{self._base_url}/v2/{path}"
        extra_headers = kwargs.pop("headers", {})
        headers = {**self._auth_headers(), **extra_headers}

        basic_auth = None
        if self._username and self._password and not self._token:
            basic_auth = (self._username, self._password)

        resp = self._session.request(
            method, url, headers=headers, auth=basic_auth, timeout=60, **kwargs
        )

        if resp.status_code == 401:
            www_auth = resp.headers.get("WWW-Authenticate", "")
            if not www_auth:
                raise RuntimeError("Registry returned 401 without a WWW-Authenticate header")
            self._fetch_token(www_auth)

            # Retry with fresh credentials
            headers = {**self._auth_headers(), **extra_headers}
            basic_auth = (
                (self._username, self._password)
                if self._username and self._password and not self._token
                else None
            )
            resp = self._session.request(
                method, url, headers=headers, auth=basic_auth, timeout=60, **kwargs
            )

        return resp

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def blob_exists(self, digest: str) -> bool:
        """Return *True* if the blob identified by *digest* already exists."""
        resp = self._request("HEAD", f"{self._namespace}/blobs/{digest}")
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return False  # unreachable

    def push_blob(self, data: bytes, digest: Optional[str] = None) -> str:
        """Push *data* as a blob and return its ``sha256:…`` digest.

        If *digest* is not provided it is computed from *data*.  The blob is
        skipped (deduplicated) if the registry already has it.
        """
        if digest is None:
            digest = sha256_digest(data)

        if self.blob_exists(digest):
            print(f"    Blob {digest[:19]}… already exists, skipping upload")
            return digest

        # Initiate a monolithic blob upload.
        resp = self._request(
            "POST",
            f"{self._namespace}/blobs/uploads/",
            headers={"Content-Length": "0"},
        )
        if resp.status_code != 202:
            raise RuntimeError(
                f"Failed to initiate blob upload (POST): "
                f"HTTP {resp.status_code} — {resp.text[:200]}"
            )

        location = resp.headers.get("Location", "")
        if not location:
            raise RuntimeError("Registry did not return a Location header after POST")

        # Resolve relative Location URLs.
        if location.startswith("/"):
            location = f"{self._base_url}{location}"

        # Append the digest query parameter.
        sep = "&" if "?" in location else "?"
        put_url = f"{location}{sep}digest={digest}"

        # Upload the blob.
        up_headers = {**self._auth_headers()}
        up_headers["Content-Type"] = "application/octet-stream"
        up_headers["Content-Length"] = str(len(data))

        basic_auth = None
        if self._username and self._password and not self._token:
            basic_auth = (self._username, self._password)

        put_resp = self._session.put(
            put_url,
            data=data,
            headers=up_headers,
            auth=basic_auth,
            timeout=300,
        )
        if put_resp.status_code not in (201, 202):
            raise RuntimeError(
                f"Failed to push blob (PUT): "
                f"HTTP {put_resp.status_code} — {put_resp.text[:200]}"
            )

        return digest

    def push_manifest(self, reference: str, manifest: dict) -> str:
        """Serialise and push *manifest*, tagged with *reference*.

        Returns the ``sha256:…`` digest of the serialised manifest JSON.
        """
        manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
        resp = self._request(
            "PUT",
            f"{self._namespace}/manifests/{reference}",
            headers={"Content-Type": OCI_MANIFEST_MEDIA_TYPE},
            data=manifest_bytes,
        )
        if resp.status_code != 201:
            raise RuntimeError(
                f"Failed to push manifest: "
                f"HTTP {resp.status_code} — {resp.text[:200]}"
            )
        return sha256_digest(manifest_bytes)


# ---------------------------------------------------------------------------
# Main push logic
# ---------------------------------------------------------------------------

def push_image(
    disk_path: str,
    config_path: str,
    reference: str,
    nvram_path: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    insecure: bool = False,
) -> None:
    """Push a tart-compatible image to an OCI registry.

    Args:
        disk_path:   Path to the raw disk image file.
        config_path: Path to the tart VM ``config.json``.
        reference:   OCI image reference, e.g. ``ghcr.io/org/image:tag``.
        nvram_path:  Optional path to the NVRAM binary.  When omitted an empty
                     NVRAM blob is pushed (tart initialises NVRAM on first run).
        username:    Registry username (optional).
        password:    Registry password (optional).
        insecure:    When *True*, use plain HTTP instead of HTTPS.
    """
    host, namespace, tag = parse_reference(reference)
    print(f"Pushing tart image to {host}/{namespace}:{tag}")

    registry = OCIRegistry(
        host,
        namespace,
        username=username,
        password=password,
        insecure=insecure,
    )

    layers = []

    # ------------------------------------------------------------------
    # 1. Config layer  (application/vnd.cirruslabs.tart.config.v1)
    # ------------------------------------------------------------------
    print("\n[1/4] Pushing VM config …")
    with open(config_path, "rb") as fh:
        config_data = fh.read()
    config_digest = registry.push_blob(config_data)
    layers.append(
        {
            "mediaType": TART_CONFIG_MEDIA_TYPE,
            "size": len(config_data),
            "digest": config_digest,
        }
    )
    print(f"      Config: {len(config_data):,} bytes → {config_digest[:19]}…")

    # ------------------------------------------------------------------
    # 2. Disk layers  (application/vnd.cirruslabs.tart.disk.v2)
    #
    # The disk is split into LAYER_LIMIT_BYTES (512 MiB) chunks, each
    # compressed with Apple's LZ4 framing format before being pushed.
    # ------------------------------------------------------------------
    disk_size = os.path.getsize(disk_path)
    print(
        f"\n[2/4] Pushing disk image ({disk_size / (1024**3):.2f} GiB) …"
        f"  (chunk size: {LAYER_LIMIT_BYTES // (1024**2)} MiB)"
    )

    total_chunks = (disk_size + LAYER_LIMIT_BYTES - 1) // LAYER_LIMIT_BYTES
    with open(disk_path, "rb") as disk_fh:
        chunk_idx = 0
        while True:
            chunk = disk_fh.read(LAYER_LIMIT_BYTES)
            if not chunk:
                break
            chunk_idx += 1
            print(
                f"  Chunk {chunk_idx}/{total_chunks}: "
                f"compressing {len(chunk) / (1024**2):.0f} MiB …",
                end="",
                flush=True,
            )

            uncompressed_digest = sha256_digest(chunk)
            compressed = apple_lz4_compress(chunk)
            compressed_digest = sha256_digest(compressed)

            ratio = len(compressed) / len(chunk) * 100
            print(f" {len(compressed) / (1024**2):.0f} MiB ({ratio:.0f}%),  pushing …", end="", flush=True)

            registry.push_blob(compressed, digest=compressed_digest)
            print(" done")

            layers.append(
                {
                    "mediaType": TART_DISK_V2_MEDIA_TYPE,
                    "size": len(compressed),
                    "digest": compressed_digest,
                    "annotations": {
                        UNCOMPRESSED_SIZE_ANNOTATION: str(len(chunk)),
                        UNCOMPRESSED_CONTENT_DIGEST_ANNOTATION: uncompressed_digest,
                    },
                }
            )

    # ------------------------------------------------------------------
    # 3. NVRAM layer  (application/vnd.cirruslabs.tart.nvram.v1)
    # ------------------------------------------------------------------
    print("\n[3/4] Pushing NVRAM …")
    if nvram_path:
        with open(nvram_path, "rb") as fh:
            nvram_data = fh.read()
        print(f"      Using NVRAM file: {nvram_path} ({len(nvram_data):,} bytes)")
    else:
        nvram_data = b""
        print(
            "      No --nvram file provided; pushing an empty NVRAM blob.\n"
            "      tart will initialise NVRAM on the first run."
        )
    nvram_digest = registry.push_blob(nvram_data)
    layers.append(
        {
            "mediaType": TART_NVRAM_MEDIA_TYPE,
            "size": len(nvram_data),
            "digest": nvram_digest,
        }
    )

    # ------------------------------------------------------------------
    # 4. OCI image config  (application/vnd.oci.image.config.v1+json)
    #
    # This is a stub config required for Docker Hub / OCI compatibility.
    # It carries the architecture, OS and any custom labels.
    # ------------------------------------------------------------------
    print("\n[4/4] Pushing OCI image config and manifest …")

    try:
        tart_cfg = json.loads(config_data)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Failed to parse config.json as JSON: {exc}") from exc

    oci_arch = tart_cfg.get("arch", "arm64")
    oci_os = _os_for_oci(tart_cfg.get("os", "darwin"))
    disk_format = tart_cfg.get("diskFormat", "raw")

    oci_config = {
        "architecture": oci_arch,
        "os": oci_os,
        "config": {
            "Labels": {
                DISK_FORMAT_LABEL: disk_format,
            }
        },
    }
    oci_config_bytes = json.dumps(oci_config, separators=(",", ":")).encode()
    oci_config_digest = registry.push_blob(oci_config_bytes)
    print(f"      OCI config: {oci_config_digest[:19]}…")

    # ------------------------------------------------------------------
    # 5. OCI manifest
    # ------------------------------------------------------------------
    upload_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST_MEDIA_TYPE,
        "config": {
            "mediaType": OCI_CONFIG_MEDIA_TYPE,
            "size": len(oci_config_bytes),
            "digest": oci_config_digest,
        },
        "layers": layers,
        "annotations": {
            UNCOMPRESSED_DISK_SIZE_ANNOTATION: str(disk_size),
            UPLOAD_TIME_ANNOTATION: upload_time,
        },
    }

    manifest_digest = registry.push_manifest(tag, manifest)
    print(
        f"\n✓ Successfully pushed tart image\n"
        f"  Reference : {host}/{namespace}:{tag}\n"
        f"  Digest    : {manifest_digest}\n"
        f"  Layers    : {len(layers)} "
        f"(1 config, {chunk_idx} disk, 1 NVRAM)"
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Push a raw disk image and tart config.json to an OCI registry "
            "in a format compatible with Cirrus Labs' tart tool."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Push to GitHub Container Registry
  push.py disk.img config.json ghcr.io/myorg/myvm:latest \\
      --nvram nvram.bin -u myuser -p $GITHUB_TOKEN

  # Push to a local registry (no TLS)
  push.py disk.img config.json localhost:5000/myvm:latest --insecure

  # Pull and run with tart after pushing
  tart pull ghcr.io/myorg/myvm:latest
  tart run myvm
""",
    )
    parser.add_argument(
        "disk",
        help="Path to the raw disk image file.",
    )
    parser.add_argument(
        "config",
        help="Path to the tart VM config.json.",
    )
    parser.add_argument(
        "reference",
        help="OCI registry reference, e.g. ghcr.io/org/image:tag.",
    )
    parser.add_argument(
        "--nvram",
        metavar="FILE",
        default=None,
        help=(
            "Path to the NVRAM binary file.  "
            "When omitted an empty NVRAM blob is pushed; "
            "tart will initialise NVRAM on first run."
        ),
    )
    parser.add_argument(
        "--username",
        "-u",
        metavar="USER",
        default=os.environ.get("REGISTRY_USERNAME"),
        help=(
            "Registry username.  "
            "Falls back to the REGISTRY_USERNAME environment variable."
        ),
    )
    parser.add_argument(
        "--password",
        "-p",
        metavar="PASS",
        default=os.environ.get("REGISTRY_PASSWORD"),
        help=(
            "Registry password / token.  "
            "Falls back to the REGISTRY_PASSWORD environment variable."
        ),
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=False,
        help="Use plain HTTP instead of HTTPS (useful for local registries).",
    )

    args = parser.parse_args()

    # Validate inputs
    if not os.path.isfile(args.disk):
        parser.error(f"Disk image not found: {args.disk}")
    if not os.path.isfile(args.config):
        parser.error(f"Config file not found: {args.config}")
    if args.nvram and not os.path.isfile(args.nvram):
        parser.error(f"NVRAM file not found: {args.nvram}")

    push_image(
        disk_path=args.disk,
        config_path=args.config,
        reference=args.reference,
        nvram_path=args.nvram,
        username=args.username,
        password=args.password,
        insecure=args.insecure,
    )


if __name__ == "__main__":
    main()
