# tart-image-formatter

A Python tool that takes a raw disk image file and a `config.json` and
pushes them to an OCI registry in a format that is fully compatible with
[Cirrus Labs' tart](https://github.com/cirruslabs/tart) macOS virtualisation
tool.  The resulting image can be pulled and run with `tart pull` / `tart run`.

## How it works

`push.py` implements tart's OCI image format exactly as described in
[DiskV2.swift](https://github.com/cirruslabs/tart/blob/main/Sources/tart/OCI/Layerizer/DiskV2.swift):

1. The tart `config.json` is pushed as an OCI blob with media type
   `application/vnd.cirruslabs.tart.config.v1`.
2. The raw disk image is split into 512 MiB chunks.  Each chunk is compressed
   with **Apple's LZ4 framing format** (the custom stream framing used by
   macOS's `Compression` framework / `NSData.compressed(using: .lz4)`) and
   pushed as a separate OCI blob with media type
   `application/vnd.cirruslabs.tart.disk.v2`.  Every disk layer carries
   `org.cirruslabs.tart.uncompressed-size` and
   `org.cirruslabs.tart.uncompressed-content-digest` annotations so that tart
   can validate the decompressed data.
3. An NVRAM blob is pushed with media type
   `application/vnd.cirruslabs.tart.nvram.v1`.  If you do not supply an NVRAM
   file the tool pushes an empty blob; tart will initialise NVRAM on the first
   run.
4. A stub OCI image config (architecture, OS, labels) and an OCI manifest are
   assembled and pushed to complete the image.

Blobs are deduplicated: if the registry already has a blob with the same
digest the upload is skipped.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`

```
requests>=2.28.0
lz4>=4.0.0
```

## Usage

```
usage: push.py [-h] [--nvram FILE] [--username USER] [--password PASS] [--insecure]
               disk config reference

positional arguments:
  disk                  Path to the raw disk image file.
  config                Path to the tart VM config.json.
  reference             OCI registry reference, e.g. ghcr.io/org/image:tag.

options:
  -h, --help            show this help message and exit
  --nvram FILE          Path to the NVRAM binary file (optional).
  --username USER, -u USER
                        Registry username (or set REGISTRY_USERNAME env var).
  --password PASS, -p PASS
                        Registry password / token (or set REGISTRY_PASSWORD env var).
  --insecure            Use plain HTTP instead of HTTPS (for local registries).
```

### Examples

```bash
# Install dependencies
pip install -r requirements.txt

# Push to GitHub Container Registry
python push.py disk.img config.json ghcr.io/myorg/myvm:latest \
    --nvram nvram.bin \
    --username myuser \
    --password "$GITHUB_TOKEN"

# Push to a local registry (no TLS)
python push.py disk.img config.json localhost:5000/myvm:latest --insecure

# Pull and run with tart after pushing
tart pull ghcr.io/myorg/myvm:latest
tart run myvm
```

## config.json format

The `config.json` file is the standard tart VM configuration file.  At a
minimum it should contain:

```json
{
  "Version": "2",
  "arch": "arm64",
  "os": "macOS",
  "diskFormat": "raw",
  "CPUCount": 4,
  "MemorySize": 4294967296
}
```

The `arch`, `os`, and `diskFormat` fields are used to populate the OCI image
config and labels.  `os` values `"macOS"` and `"darwin"` are both recognised
and mapped to `"darwin"` in the OCI config.

## Authentication

Credentials can be provided via:
- `--username` / `--password` flags
- `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` environment variables

Both Bearer-token and Basic authentication schemes are supported.