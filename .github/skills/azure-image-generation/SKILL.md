---
name: azure-image-generation
description: "Generate or edit bitmap images with the user's Azure OpenAI gpt-image-2 deployment. Use for text-to-image generation, image-to-image editing, inpainting with masks, reference-image transformations, restyling, removing or replacing objects or backgrounds, sprites, game assets, icons, textures, and transparent PNG/WebP output."
argument-hint: "Describe the image to generate or edit, input image if any, and output path"
---

# Azure Image Generation and Editing

Generate new images or edit existing images through the user's Azure OpenAI-compatible Images API deployment.

## Service Configuration

- Generation endpoint: `AZURE_IMAGE_ENDPOINT` or the `--endpoint` argument (required)
- Edit endpoint: `AZURE_IMAGE_EDIT_ENDPOINT` or derived automatically from the generation endpoint
- Deployment: `gpt-image-2`
- Authentication: Microsoft Entra ID through the signed-in Azure CLI account
- Token scope: `https://ai.azure.com/.default`
- Tenant: active Azure CLI tenant, or optional `AZURE_TENANT_ID`

Never request, print, or store an API key. If authentication fails, ask the user to run `az login` directly in their terminal.

## Workflow

1. Determine whether the user wants text-to-image generation or an edit. For an edit, identify the input image(s), optional mask, output path, and which visual details must remain unchanged.
2. Resolve the prompt, output path, orientation, and whether transparency is needed. Infer reasonable visual details when the request is already clear.
3. Write a concrete prompt that describes subject, composition, medium, lighting, palette, camera, desired changes, preservation requirements, and relevant exclusions.
4. Run [generate_image.py](./scripts/generate_image.py) with Python 3. With no `--input`, it generates from text. One or more `--input` options switch it to multipart image-edit mode. The script has no third-party Python dependencies.
5. Inspect the generated file with the image-viewing tool. Do not report success before checking that the image is nonblank and satisfies the request.
6. Iterate with a revised prompt when the result misses an explicit requirement.

## Command

```bash
python3 "$PWD/.github/skills/azure-image-generation/scripts/generate_image.py" \
  --endpoint "$AZURE_IMAGE_ENDPOINT" \
  --prompt "A precise description of the requested image" \
  --output "$PWD/path/to/image.png"
```

Edit an existing image while naming what must be preserved:

```bash
python3 "$PWD/.github/skills/azure-image-generation/scripts/generate_image.py" \
  --endpoint "$AZURE_IMAGE_ENDPOINT" \
  --input "$PWD/path/to/source.png" \
  --prompt "Replace the cloudy sky with a warm sunset; preserve the person, pose, framing, and foreground exactly" \
  --output "$PWD/path/to/edited.png" \
  --quality high
```

For multiple reference images, repeat `--input` up to 16 times. To inpaint a region, add a PNG mask whose fully transparent pixels mark the area to replace:

```bash
python3 "$PWD/.github/skills/azure-image-generation/scripts/generate_image.py" \
  --input "$PWD/path/to/source.png" \
  --mask "$PWD/path/to/mask.png" \
  --prompt "Remove the object inside the masked area and reconstruct the background" \
  --output "$PWD/path/to/edited.png"
```

For a sprite or composited asset, request a transparent background explicitly:

```bash
python3 "$PWD/.github/skills/azure-image-generation/scripts/generate_image.py" \
  --prompt "Full-body pixel-art clinician sprite, centered, isolated, no shadow" \
  --output "$PWD/data/sprites/clinician.png" \
  --background transparent \
  --quality high
```

Use `--size 1536x1024` for landscape images and `--size 1024x1536` for portrait images. Supported output formats are PNG, JPEG, and WebP; the format is inferred from the output extension. Edit inputs must be PNG or JPEG, each under 50 MB. `--edit-endpoint` can override the automatically derived edit URL. Use `--dry-run` to validate request metadata without authenticating, uploading inputs, or generating an image.

## Output Rules

- Save project assets inside the current workspace unless the user requests another location.
- For edits, never overwrite the input unless the user explicitly requests it; default to a new output path.
- Preserve the user's requested subjects, layout, identity, text, and style unless the prompt explicitly changes them.
- Prefer PNG or WebP for transparent assets.
- Do not use JPEG with `--background transparent`.
- Preserve the full-resolution generated image unless the user requests resizing or cropping.
- Surface Azure content-policy, authorization, quota, and request-validation errors verbatim enough to act on, but never expose access tokens.