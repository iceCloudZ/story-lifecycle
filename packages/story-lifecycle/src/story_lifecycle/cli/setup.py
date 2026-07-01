"""First-run setup wizard — guide user to configure LLM API key.

Config-file IO primitives (get/save/merge) live in the top-level
`config.py` infra module (moved there in ISS-006 to break the layering
inversion where `sources/`/`context_providers/`/`orchestrator/` depended on
`cli/` just to read config). They are re-imported here for the entry-layer
helpers and for backward-compatible imports.
"""

import os
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel

from ..infra.config import (
    CONFIG_DIR,
    CONFIG_FILE,
    _merge_config,
    get_config,
    save_config,
)

console = Console()

PRESET_PROVIDERS = {
    "1": {
        "name": "deepseek",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
    },
    "2": {
        "name": "anthropic",
        "base_url": "https://api.anthropic.com",
        "models": [
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "claude-haiku-4-5-20251001",
        ],
    },
    "3": {
        "name": "openai",
        "base_url": "https://api.openai.com",
        "models": ["gpt-4o", "gpt-4o-mini"],
    },
    "4": {
        "name": "zhipu",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-flash"],
    },
    "5": {
        "name": "custom",
        "base_url": "",
        "models": [],
    },
}


def is_configured() -> bool:
    """Check if the user has completed setup."""
    if not CONFIG_FILE.exists():
        return False
    try:
        config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return False
    return bool(config.get("api_key"))


def run_setup():
    """Interactive setup wizard. Idempotent — re-runs will update existing config."""
    console.print()
    console.print(
        Panel.fit(
            "[bold]Welcome to Story Lifecycle Manager![/]\n\n"
            "This tool orchestrates AI coding assistants to take your story\n"
            "from requirements all the way to production.\n\n"
            "To get started, you'll need an LLM API key.\n"
            "The orchestrator uses this for routing decisions (Phase 2),\n"
            "and your AI coding tools (Claude Code, Codex, etc.) use their own keys.",
            title="Setup",
            border_style="green",
        )
    )
    console.print()

    # Step 1: Choose provider
    existing = get_config()
    current_provider = existing.get("provider", "deepseek")

    console.print("[bold]Step 1: Choose your LLM provider[/]")
    console.print("This key is used by the orchestrator for routing decisions.\n")
    for key, provider in PRESET_PROVIDERS.items():
        marker = " (current)" if provider["name"] == current_provider else ""
        console.print(f"  [{key}] {provider['name']}{marker}")
    console.print()

    choice = console.input("Choice [1-5] (default: 1): ").strip() or "1"
    if choice not in PRESET_PROVIDERS:
        choice = "1"

    provider_info = PRESET_PROVIDERS[choice]

    # Step 2: API Key
    console.print()
    console.print("[bold]Step 2: Enter your API key[/]")

    current_key = existing.get("api_key", "")
    masked = current_key[:8] + "****" if current_key else ""
    hint = f" (current: {masked})" if masked else ""

    api_key = console.input(f"API Key{hint}: ", password=False).strip()

    if not api_key and current_key:
        api_key = current_key  # keep existing

    if not api_key and current_key:
        api_key = current_key  # keep existing

    if not api_key:
        console.print("\n[red]API key is required to continue.[/]")
        console.print("Press Ctrl+C to exit, or re-run [bold]story setup[/] later.\n")
        return

    # Step 3: Base URL (for custom provider)
    base_url = provider_info["base_url"]
    if choice == "5":
        console.print()
        console.print("[bold]Step 3: Enter your API base URL[/]")
        base_url = console.input(
            "Base URL: ",
        ).strip()

    # Step 4: Model
    console.print()
    console.print("[bold]Step 4: Choose model for orchestrator routing[/]")

    models = provider_info["models"]
    if models:
        for i, m in enumerate(models, 1):
            console.print(f"  [{i}] {m}")
        default_model = existing.get("model", models[0])
        model_choice = console.input(f"Model (default: {default_model}): ").strip()
        if model_choice.isdigit() and 1 <= int(model_choice) <= len(models):
            model = models[int(model_choice) - 1]
        elif model_choice:
            model = model_choice
        else:
            model = default_model
    else:
        model = console.input("Model name: ").strip()

    # Optional Step 5: Vision model for PRD intake with images
    console.print()
    console.print(
        "[bold]Step 5 (Optional): Configure vision model for image understanding[/]"
    )
    console.print(
        "Used when a TAPD story/bug contains screenshots or diagrams.\n"
        "Leave empty to use the main model if it supports vision, or disable image understanding."
    )
    existing_vision = existing.get("vision", {})
    vision_provider = console.input(
        "Vision Provider (openai-compatible | kimi-cli, default: openai-compatible): "
    ).strip() or existing_vision.get("provider", "openai-compatible")
    vision_api_key = console.input(
        f"Vision API Key (current: {'set' if existing_vision.get('api_key') else 'none'}): "
    ).strip()
    vision_base_url = (
        console.input(f"Vision Base URL (default: {base_url}): ").strip() or base_url
    )
    vision_model = console.input(
        "Vision Model (e.g. gpt-4o, qwen-vl-max, kimi-for-coding): "
    ).strip()
    vision_config = {}
    if vision_provider:
        vision_config["provider"] = vision_provider
    if vision_api_key:
        vision_config["api_key"] = vision_api_key
    if vision_base_url:
        vision_config["base_url"] = vision_base_url
    if vision_model:
        vision_config["model"] = vision_model

    # Save config
    config: dict = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "provider": provider_info["name"],
    }
    if vision_config:
        config["vision"] = vision_config
    save_config(config)

    # Set env vars for current session
    os.environ["STORY_LLM_API_KEY"] = api_key
    os.environ["STORY_LLM_BASE_URL"] = base_url
    os.environ["STORY_LLM_MODEL"] = model
    if vision_config.get("provider"):
        os.environ["STORY_VISION_PROVIDER"] = vision_config["provider"]
    if vision_config.get("api_key"):
        os.environ["STORY_VISION_API_KEY"] = vision_config["api_key"]
    if vision_config.get("base_url"):
        os.environ["STORY_VISION_BASE_URL"] = vision_config["base_url"]
    if vision_config.get("model"):
        os.environ["STORY_VISION_MODEL"] = vision_config["model"]

    console.print()
    vision_line = (
        f"Vision: {vision_config.get('model', 'disabled')}\n" if vision_config else ""
    )
    console.print(
        Panel.fit(
            f"[green]Setup complete![/]\n\n"
            f"Provider: {provider_info['name']}\n"
            f"Model: {model}\n"
            f"{vision_line}"
            f"Config: {CONFIG_FILE}\n\n"
            f"Run [bold]story[/] to start the board.\n"
            f"Re-run [bold]story setup[/] anytime to change settings.",
            border_style="green",
        )
    )


def load_config_to_env():
    """Load config from file and set env vars. Called on CLI startup."""
    config = get_config()
    if not config:
        return

    if config.get("api_key") and not os.environ.get("STORY_LLM_API_KEY"):
        os.environ["STORY_LLM_API_KEY"] = config["api_key"]
    if config.get("base_url") and not os.environ.get("STORY_LLM_BASE_URL"):
        os.environ["STORY_LLM_BASE_URL"] = config["base_url"]
    if config.get("model") and not os.environ.get("STORY_LLM_MODEL"):
        os.environ["STORY_LLM_MODEL"] = config["model"]

    # Also load optional vision config so intake image understanding works.
    load_vision_config_to_env()
    load_gitlab_config_to_env()


def load_gitlab_config_to_env():
    """Load GitLab integration config from file and set env vars."""
    config = get_config()
    gitlab = config.get("gitlab") or {}
    if not gitlab:
        return

    if gitlab.get("token") and not os.environ.get("GITLAB_TOKEN"):
        os.environ["GITLAB_TOKEN"] = gitlab["token"]
    if gitlab.get("url") and not os.environ.get("GITLAB_URL"):
        os.environ["GITLAB_URL"] = gitlab["url"]


def save_gitlab_config(token: str = "", url: str = "") -> None:
    """Persist GitLab token/url to config file."""
    existing = get_config()
    gitlab = dict(existing.get("gitlab") or {})
    if token:
        gitlab["token"] = token
    if url:
        gitlab["url"] = url
    if gitlab:
        save_config({"gitlab": gitlab})


def load_vision_config_to_env():
    """Load optional vision config from file and set env vars."""
    config = get_config()
    vision = config.get("vision") or {}
    if not vision:
        return

    if vision.get("provider") and not os.environ.get("STORY_VISION_PROVIDER"):
        os.environ["STORY_VISION_PROVIDER"] = vision["provider"]
    if vision.get("api_key") and not os.environ.get("STORY_VISION_API_KEY"):
        os.environ["STORY_VISION_API_KEY"] = vision["api_key"]
    if vision.get("base_url") and not os.environ.get("STORY_VISION_BASE_URL"):
        os.environ["STORY_VISION_BASE_URL"] = vision["base_url"]
    if vision.get("model") and not os.environ.get("STORY_VISION_MODEL"):
        os.environ["STORY_VISION_MODEL"] = vision["model"]


DEFAULT_SUB_TYPES = {
    "bug-fix": {
        "label": "缺陷修复",
        "color": "red",
        "default_start_stage": "implement",
        "description_template": "修复以下问题：",
    },
    "integration": {
        "label": "联调适配",
        "color": "yellow",
        "default_start_stage": "implement",
        "description_template": "前后端联调修改：",
    },
    "refinement": {
        "label": "需求补充",
        "color": "blue",
        "default_start_stage": "design",
        "description_template": "需求补充/调整：",
    },
    "redo": {
        "label": "返工重做",
        "color": "orange",
        "default_start_stage": "design",
        "description_template": "重做，原因：",
    },
}


def get_sub_types() -> dict:
    """Load sub-story types: defaults merged with user config."""
    config = get_config()
    types = dict(DEFAULT_SUB_TYPES)
    user_types = config.get("sub_story_types", {})
    if user_types:
        types.update(user_types)
    return types
