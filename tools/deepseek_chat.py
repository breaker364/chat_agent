"""
简单的一文一答 DeepSeek 对话脚本 (OpenAI 兼容 SDK).

用法:
    python deepseek_chat.py
    python deepseek_chat.py --config path/to/config.json

对话中:
    - 直接输入问题, 回车送出, 等待回答
    - 输入 /exit 或 /quit 退出
    - 输入 /clear 清空对话历史
    - 输入 /history 查看历史
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# --- 1. 加载配置 ---

DEFAULT_CONFIG_PATH = Path(
    r"D:\NoobhekProject\cae-fusion-demo\code\LLM_tools\llm_provider_deepseek.json"
)


def load_config(config_path: str | Path | None = None) -> dict[str, str]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"配置文件未找到: {resolved}")
    data = json.loads(resolved.read_text(encoding="utf-8"))
    for key in ("base_url", "api_key", "model"):
        if key not in data or not str(data.get(key, "")).strip():
            raise ValueError(f"配置缺少 '{key}': {resolved}")
    return {
        "base_url": str(data["base_url"]).rstrip("/"),
        "api_key": str(data["api_key"]),
        "model": str(data["model"]),
    }


# --- 2. 核心对话逻辑 ---

SYSTEM_PROMPT = "你是一个有帮助的AI助手。请用简洁、准确的中文回答问题。"


def chat_once(
    client,
    model: str,
    messages: list[dict[str, str]],
) -> tuple[str, int, int]:
    """发送一轮对话, 返回 (回答文本, 输入token数, 输出token数)."""
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.7,
        max_tokens=4096,
    )
    choice = response.choices[0]
    content = choice.message.content or ""
    usage = response.usage
    input_tokens = usage.prompt_tokens if usage else 0
    output_tokens = usage.completion_tokens if usage else 0
    return content, input_tokens, output_tokens


def print_separator(char: str = "─", width: int = 60) -> None:
    print(char * width)


def main() -> None:
    parser = argparse.ArgumentParser(description="DeepSeek 一文一答对话")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="LLM 配置文件路径 (JSON)",
    )
    args = parser.parse_args()

    # 加载配置
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"模型: {cfg['model']}")
    print(f"地址: {cfg['base_url']}")
    print_separator()

    # 创建 OpenAI 兼容客户端
    import openai

    client = openai.OpenAI(
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
    )

    # 对话消息列表 (system prompt + 历史 + 当前问题)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    total_input_tokens = 0
    total_output_tokens = 0

    print("开始对话 (输入 /exit 退出, /clear 清空历史, /history 查看历史)")
    print_separator()

    while True:
        # 读取用户输入
        try:
            user_input = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n再见!")
            break

        if not user_input:
            continue

        # 处理命令
        if user_input.lower() in {"/exit", "/quit", "/q"}:
            print("再见!")
            break
        elif user_input.lower() == "/clear":
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            print("[历史已清空]")
            continue
        elif user_input.lower() == "/history":
            print_separator("-")
            for i, msg in enumerate(messages):
                role = msg["role"]
                content = msg["content"]
                # 截断过长的内容
                preview = content[:200] + "..." if len(content) > 200 else content
                print(f"[{i}] {role}: {preview}")
            print_separator("-")
            continue

        # 添加用户消息
        messages.append({"role": "user", "content": user_input})

        # 调用 API
        print("\nAI: ", end="", flush=True)
        try:
            answer, in_tok, out_tok = chat_once(client, cfg["model"], messages)
            print(answer)
            total_input_tokens += in_tok
            total_output_tokens += out_tok
            print(f"\n[tokens: 输入{in_tok} 输出{out_tok} | 累计: 输入{total_input_tokens} 输出{total_output_tokens}]")
        except Exception as e:
            print(f"\n[调用失败] {e}")
            # 移除刚才添加的用户消息, 避免重试时重复
            messages.pop()
            continue

        # 添加助手回复到历史
        messages.append({"role": "assistant", "content": answer})


if __name__ == "__main__":
    main()
