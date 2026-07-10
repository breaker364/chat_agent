# Multimodal Model Configuration

The main reasoning model remains DeepSeek. Images are analyzed by a separate
OpenAI-compatible multimodal model, and its textual result is supplied to the
main Agent for verification and summarization.

## Runtime Configuration

Edit `config.json`:

```json
{
  "vision": {
    "enabled": true,
    "base_url": "https://your-vision-endpoint.example/v1",
    "model": "your-vision-model",
    "api_key_env": "VISION_API_KEY",
    "max_images_per_request": 4,
    "max_image_bytes": 20971520,
    "max_output_tokens": 2048,
    "timeout_seconds": 60
  }
}
```

Set the API key in the environment instead of committing it:

```powershell
$env:VISION_API_KEY = "your-api-key"
```

Restart the backend after changing configuration.

## Request Flow

1. The frontend uploads an image into `tmp/uploads/<session>/`.
2. The user message contains the workspace-relative image path.
3. The Agent detects supported image paths before DeepSeek is called.
4. The configured multimodal model analyzes the image.
5. The visual result is appended to the user request as evidence.
6. DeepSeek combines the visual evidence with tools, files, and conversation
   context to produce the final answer.

The main Agent can also call the `analyze_image` tool later when an image is
discovered during file inspection.

## Supported Image Types

- PNG
- JPEG
- WEBP
- GIF
- BMP

The configured endpoint must support OpenAI-compatible chat messages whose
`content` is a list containing `text` and `image_url` items.
