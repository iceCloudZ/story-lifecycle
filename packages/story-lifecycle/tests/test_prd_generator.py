from story_lifecycle.orchestrator.service import prd_generator


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.prompt = ""

    def invoke_structured(self, prompt, schema, **kwargs):
        self.prompt = prompt
        return schema.model_validate(self.payload)


def test_prd_generator_prompt_is_self_contained_and_source_agnostic(monkeypatch):
    llm = FakeLLM(
        {
            "action": "generated",
            "dingtalk_links": [],
            "markdown": "# PRD\n\n## 安全审查\n\n无前端可控核心参数。",
            "summary": "ok",
        }
    )
    monkeypatch.setattr(prd_generator, "get_llm", lambda: llm)

    result = prd_generator.generate_prd_from_source(
        prd_generator.StorySourceSnapshot(
            story_key="tapd-1",
            source_type="tapd",
            source_id="1",
            title="测试需求",
            url="https://tapd.example/story/1",
            description="TAPD 正文",
        )
    )

    assert result.action == "generated"
    assert "prd-generator" in llm.prompt
    assert "不要依赖外部 hc-all skill" in llm.prompt
    assert "source_type: tapd" in llm.prompt
    assert "TAPD 正文" in llm.prompt


def test_prd_generator_prompt_prefers_lightweight_intake_prd():
    prompt = prd_generator.build_prd_generator_prompt(
        prd_generator.StorySourceSnapshot(
            story_key="tapd-1",
            source_type="tapd",
            source_id="1",
            title="测试需求",
            description="活动创建需要替换人群接口",
        )
    )

    assert "轻量 Intake PRD" in prompt
    assert "800-1500" in prompt
    assert "不要补充来源中没有的接口名、字段名、默认值、性能指标" in prompt
    assert "这些留给后续 Design/Build/Verify 阶段" in prompt
    assert "安全审查为必填" not in prompt
    assert "非功能与兼容性要求" not in prompt


def test_prd_generator_can_return_manual_download_required(monkeypatch):
    llm = FakeLLM(
        {
            "action": "manual_download_required",
            "dingtalk_links": ["https://alidocs.dingtalk.com/i/nodes/doc"],
            "markdown": "",
            "summary": "需要人工下载钉钉文档",
        }
    )
    monkeypatch.setattr(prd_generator, "get_llm", lambda: llm)

    result = prd_generator.generate_prd_from_source(
        prd_generator.StorySourceSnapshot(
            story_key="tapd-2",
            source_type="tapd",
            source_id="2",
            title="钉钉需求",
            description="见钉钉链接",
        )
    )

    assert result.action == "manual_download_required"
    assert result.dingtalk_links == ["https://alidocs.dingtalk.com/i/nodes/doc"]
