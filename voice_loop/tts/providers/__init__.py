"""TTS 提供者（每个文件一个后端，注册表用 pkgutil 自动扫描）。

加一个新后端 = 在这个目录里加一个文件，模块顶层交出一个 :class:`..spec.ProviderSpec`：

```python
PROVIDER = ProviderSpec(
    id="my-tts", label="我的 TTS",
    capabilities=frozenset({CAP_VOICE_ID, CAP_STREAM}),
    fields=(FieldSpec("voice", "音色", "select", options=("a", "b")),),
    ready=lambda settings: (True, ""),
    create=lambda settings, spec: MyEngine(settings, spec),
    apply=lambda settings, spec: _apply(settings, spec),
)
```

★顶层不要 import 引擎/第三方库★（`create()` 里再 import）：注册=扫一遍目录，
不该为了列个表单就把 piper / sherpa / edge-tts 全加载进来。
"""
