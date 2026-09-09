from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

DEFAULT_MODEL = "gpt-image-2"
DEFAULT_TOKEN_SCOPE = "https://ai.azure.com/.default"
MAX_INPUT_IMAGES = 16
MAX_INPUT_FILE_BYTES = 50 * 1024 * 1024
FORMAT_BY_SUFFIX = {
    ".jpeg": "jpeg",
    ".jpg": "jpeg",
    ".png": "png",
    ".webp": "webp",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate or edit an image with Azure OpenAI and save it locally."
    )
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="Image-generation prompt.")
    prompt_group.add_argument(
        "--prompt-file",
        type=Path,
        help="UTF-8 prompt file, or '-' to read the prompt from standard input.",
    )
    parser.add_argument("--output", required=True, type=Path, help="Output image path.")
    parser.add_argument(
        "--input",
        dest="input_images",
        action="append",
        type=Path,
        default=[],
        help=(
            "PNG or JPEG image to edit. Repeat for multiple reference images. "
            "Omit to generate an image from text."
        ),
    )
    parser.add_argument(
        "--mask",
        type=Path,
        help="Optional PNG mask for editing; transparent areas are replaced.",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get(
            "AZURE_IMAGE_ENDPOINT",
            os.environ.get("AZURE_IMAGE_GENERATION_ENDPOINT", ""),
        ),
        help="Image generation endpoint.",
    )
    parser.add_argument(
        "--edit-endpoint",
        default=os.environ.get("AZURE_IMAGE_EDIT_ENDPOINT"),
        help=(
            "Image edit endpoint. By default, derive it by replacing "
            "'/images/generations' with '/images/edits'."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("AZURE_IMAGE_GENERATION_MODEL", DEFAULT_MODEL),
        help="Azure deployment name.",
    )
    parser.add_argument(
        "--tenant",
        default=os.environ.get("AZURE_TENANT_ID"),
        help="Optional Microsoft Entra tenant used by Azure CLI authentication.",
    )
    parser.add_argument(
        "--token-scope",
        default=os.environ.get("AZURE_IMAGE_TOKEN_SCOPE", DEFAULT_TOKEN_SCOPE),
        help="Microsoft Entra scope used by Azure CLI authentication.",
    )
    parser.add_argument(
        "--size",
        choices=("auto", "1024x1024", "1536x1024", "1024x1536"),
        default="1024x1024",
    )
    parser.add_argument(
        "--quality",
        choices=("auto", "low", "medium", "high"),
        default="medium",
    )
    parser.add_argument(
        "--background",
        choices=("auto", "opaque", "transparent"),
        default="auto",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print request metadata without authenticating or sending image data.",
    )
    arguments = parser.parse_args()
    if not arguments.endpoint:
        parser.error("Pass --endpoint or set AZURE_IMAGE_ENDPOINT.")
    return arguments


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        prompt = args.prompt
    elif str(args.prompt_file) == "-":
        prompt = sys.stdin.read()
    else:
        prompt = args.prompt_file.read_text(encoding="utf-8")
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("The image prompt must not be empty.")
    return prompt


def output_format(path: Path, background: str) -> str:
    image_format = FORMAT_BY_SUFFIX.get(path.suffix.casefold())
    if image_format is None:
        supported = ", ".join(sorted(FORMAT_BY_SUFFIX))
        raise ValueError(f"Output must use one of these extensions: {supported}")
    if background == "transparent" and image_format == "jpeg":
        raise ValueError("Transparent output requires PNG or WebP, not JPEG.")
    return image_format


def edit_endpoint(generation_endpoint: str) -> str:
    parsed = urllib_parse.urlsplit(generation_endpoint)
    path = parsed.path.rstrip("/")
    generation_suffix = "/images/generations"
    if not path.endswith(generation_suffix):
        raise ValueError(
            "Could not derive the edit endpoint. Pass --edit-endpoint explicitly."
        )
    edit_path = f"{path[:-len(generation_suffix)]}/images/edits"
    return urllib_parse.urlunsplit(parsed._replace(path=edit_path))


def validate_edit_files(
    input_images: list[Path],
    mask: Path | None,
) -> tuple[list[Path], Path | None]:
    if mask is not None and not input_images:
        raise ValueError("--mask requires at least one --input image.")
    if len(input_images) > MAX_INPUT_IMAGES:
        raise ValueError(f"Image editing supports at most {MAX_INPUT_IMAGES} inputs.")

    validated: list[Path] = []
    for path in input_images:
        path = path.expanduser()
        if path.suffix.casefold() not in {".jpeg", ".jpg", ".png"}:
            raise ValueError(f"Edit input must be PNG or JPEG: {path}")
        if not path.is_file():
            raise ValueError(f"Edit input does not exist: {path}")
        if path.stat().st_size > MAX_INPUT_FILE_BYTES:
            raise ValueError(f"Edit input exceeds 50 MB: {path}")
        validated.append(path)

    if mask is not None:
        mask = mask.expanduser()
        if mask.suffix.casefold() != ".png":
            raise ValueError("The edit mask must be a PNG file.")
        if not mask.is_file():
            raise ValueError(f"Edit mask does not exist: {mask}")
        if mask.stat().st_size > MAX_INPUT_FILE_BYTES:
            raise ValueError(f"Edit mask exceeds 50 MB: {mask}")
    return validated, mask


def get_access_token(tenant: str | None, token_scope: str) -> str:
    command = [
        "az",
        "account",
        "get-access-token",
        "--scope",
        token_scope,
        "--output",
        "json",
    ]
    if tenant:
        command.extend(("--tenant", tenant))
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            "Azure CLI is required. Install 'az', then authenticate with 'az login'."
        ) from error
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip() or error.stdout.strip() or str(error)
        raise RuntimeError(f"Azure CLI authentication failed: {message}") from error

    token = json.loads(result.stdout).get("accessToken")
    if not token:
        raise RuntimeError("Azure CLI returned no access token.")
    return str(token)


def send_request(request: urllib_request.Request, timeout: int) -> dict[str, Any]:
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        try:
            details = json.loads(response_body)
            message = details.get("error", {}).get("message", response_body)
        except json.JSONDecodeError:
            message = response_body
        if error.code in (401, 403):
            message += (
                " Ensure the signed-in identity has the Cognitive Services OpenAI "
                "User role on the resource."
            )
        raise RuntimeError(f"Azure image request failed ({error.code}): {message}") from error
    except urllib_error.URLError as error:
        raise RuntimeError(f"Could not reach the Azure image endpoint: {error.reason}") from error


def request_json(
    endpoint: str,
    payload: dict[str, Any],
    token: str,
    timeout: int,
) -> dict[str, Any]:
    request = urllib_request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    return send_request(request, timeout)


def multipart_data(
    fields: dict[str, str],
    files: list[tuple[str, Path]],
) -> tuple[bytes, str]:
    boundary = f"----azure-image-{uuid.uuid4().hex}"
    delimiter = f"--{boundary}\r\n".encode("ascii")
    body: list[bytes] = []
    for name, value in fields.items():
        body.extend(
            (
                delimiter,
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    for name, path in files:
        filename = path.name.replace('"', "_").replace("\r", "").replace("\n", "")
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body.extend(
            (
                delimiter,
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
                path.read_bytes(),
                b"\r\n",
            )
        )
    body.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(body), f"multipart/form-data; boundary={boundary}"


def request_edit(
    endpoint: str,
    fields: dict[str, str],
    files: list[tuple[str, Path]],
    token: str,
    timeout: int,
) -> dict[str, Any]:
    body, content_type = multipart_data(fields, files)
    request = urllib_request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        },
        method="POST",
    )
    return send_request(request, timeout)


def image_bytes(response: dict[str, Any], timeout: int) -> bytes:
    data = response.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise RuntimeError("Azure returned no image data.")

    image = data[0]
    encoded = image.get("b64_json")
    if isinstance(encoded, str):
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise RuntimeError("Azure returned invalid base64 image data.") from error

    image_url = image.get("url")
    if isinstance(image_url, str):
        try:
            with urllib_request.urlopen(image_url, timeout=timeout) as response_stream:
                return response_stream.read()
        except urllib_error.URLError as error:
            raise RuntimeError(f"Could not download the generated image: {error.reason}") from error

    raise RuntimeError("Azure response contained neither 'b64_json' nor 'url'.")


def main() -> int:
    args = parse_args()
    try:
        prompt = read_prompt(args)
        image_format = output_format(args.output, args.background)
        request_fields = {
            "model": args.model,
            "prompt": prompt,
            "size": args.size,
            "quality": args.quality,
            "background": args.background,
            "output_format": image_format,
        }
        input_images, mask = validate_edit_files(args.input_images, args.mask)
        if input_images:
            endpoint = args.edit_endpoint or edit_endpoint(args.endpoint)
            image_field = "image" if len(input_images) == 1 else "image[]"
            files = [(image_field, path) for path in input_images]
            if mask is not None:
                files.append(("mask", mask))
            if args.dry_run:
                print(
                    json.dumps(
                        {
                            "mode": "edit",
                            "endpoint": endpoint,
                            "fields": request_fields,
                            "files": [
                                {"field": field, "path": str(path)}
                                for field, path in files
                            ],
                        },
                        indent=2,
                    )
                )
                return 0

            token = get_access_token(args.tenant, args.token_scope)
            response = request_edit(
                endpoint,
                request_fields,
                files,
                token,
                args.timeout,
            )
        else:
            endpoint = args.endpoint
            if args.dry_run:
                print(
                    json.dumps(
                        {
                            "mode": "generate",
                            "endpoint": endpoint,
                            "payload": request_fields,
                        },
                        indent=2,
                    )
                )
                return 0

            token = get_access_token(args.tenant, args.token_scope)
            response = request_json(
                endpoint,
                request_fields,
                token,
                args.timeout,
            )
        generated_image = image_bytes(response, args.timeout)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(generated_image)
        print(args.output.expanduser().resolve())
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())