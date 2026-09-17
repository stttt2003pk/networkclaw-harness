from networkclaw_harness.projection import project_mapping


def test_projection_redacts_nested_secrets_but_preserves_visible_metadata():
    projected = project_mapping(
        {
            "tool": "web.fetch",
            "authorization": "Bearer secret",
            "nested": {"system_prompt": "hidden", "status": "completed"},
        }
    )

    assert projected == {
        "tool": "web.fetch",
        "authorization": "[redacted]",
        "nested": {"system_prompt": "[redacted]", "status": "completed"},
    }

