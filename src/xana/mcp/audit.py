"""MCP 工具定义的确定性安全粗审(design §8.2/§8.3)。

纯函数:同一原始定义输出相同 finding;不读时钟、不随机、不调用外部服务。
finding 只含稳定 rule ID、JSON path 与静态说明,绝不回显可疑原文。结构非法与
扫描器异常都产出拒绝 finding(fail closed),不抛出后放行。

扫描范围:顶层 name/description,以及 inputSchema/outputSchema 内的所有键和
字符串值(含嵌套 properties、enum、examples、default、title)。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

RAW_NAME_MAX = 128
TOOL_DEF_MAX_BYTES = 32 * 1024
DESCRIPTION_MAX_CHARS = 4000

_JSON_SEPARATORS = (",", ":")


@dataclass(frozen=True)
class AuditFinding:
    """一次审核命中;rule 为稳定 ID,path 为 JSON path,message 为静态说明。"""

    rule: str
    path: str
    message: str


_MESSAGES = {
    "struct.invalid_type": "工具定义必须是 JSON 对象。",
    "struct.name": "工具名缺失、为空或不是字符串。",
    "struct.name_too_long": f"原始工具名超过 {RAW_NAME_MAX} 字符。",
    "struct.input_schema": "inputSchema 必须是对象类型的 JSON Schema。",
    "struct.output_schema": "outputSchema 若存在必须是对象类型的 JSON Schema。",
    "struct.not_serializable": "工具定义包含无法 JSON 序列化的值。",
    "struct.too_large": "工具定义超过 32 KiB,已隔离而非截断放行。",
    "struct.description_too_long": f"description 超过 {DESCRIPTION_MAX_CHARS} 字符。",
    "inject.ignore_instructions": "包含要求忽略/覆盖先前指令的特征文本。",
    "inject.fake_system_tag": "包含伪造 system/developer 或特殊对话控制标签。",
    "inject.credential_leak": "包含要求泄露凭据、系统提示词、环境变量或本地内容的特征文本。",
    "inject.bypass_approval": "包含绕过审批/安全策略的特征文本。",
    "inject.hidden_action": "包含要求对用户隐藏操作的特征文本。",
    "inject.unrelated_tools": "包含诱导调用无关工具的特征文本。",
    "inject.hidden_unicode": "包含零宽或双向控制字符。",
    "audit.scan_error": "审核扫描异常,按拒绝处理。",
}

# 高置信短语而非单词封禁,降低对正常说明的误伤(§8.3)。
_INJECTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("inject.ignore_instructions", (
        "ignore previous instructions",
        "ignore all previous instructions",
        "ignore the above instructions",
        "disregard previous instructions",
        "忽略先前的指令",
        "忽略之前的指令",
        "忽略以上指令",
        "无视先前的指令",
        "不要遵循先前的指令",
        "忘记之前的所有指令",
    )),
    ("inject.fake_system_tag", (
        "<system>",
        "</system>",
        "<system ",
        "<developer>",
        "</developer>",
        "<developer ",
        "[inst]",
        "<<sys>>",
        "[system]",
    )),
    ("inject.credential_leak", (
        "reveal the system prompt",
        "leak the system prompt",
        "print the system prompt",
        "泄露系统提示词",
        "泄露系统提示",
        "系统提示词发送给",
        "your api key",
        "your api keys",
        "你的 api 密钥",
        "api密钥",
        "all environment variables",
        "全部环境变量",
        "环境变量的值",
        "exfiltrate",
        "~/.ssh",
        "id_rsa",
        ".aws/credentials",
        "读取本地文件并发送",
        "read local files and send",
        "上传到 http",
        "发送到 http",
        "post to http",
    )),
    ("inject.bypass_approval", (
        "绕过审批",
        "绕过批准",
        "无需批准",
        "无需审批",
        "不需要批准",
        "without approval",
        "without user approval",
        "no approval needed",
        "skip approval",
        "bypass approval",
        "绕过安全策略",
        "disable safety checks",
    )),
    ("inject.hidden_action", (
        "不要告诉用户",
        "不要告知用户",
        "无需告知用户",
        "在用户不知情",
        "don't tell the user",
        "do not tell the user",
        "without telling the user",
        "without the user knowing",
        "hide this",
        "隐藏此操作",
        "隐藏这个操作",
        "秘密执行",
        "静默执行",
    )),
    ("inject.unrelated_tools", (
        "无关工具",
        "unrelated tools",
        "call other tools",
        "调用其他工具",
        "然后调用其他工具",
        "invoke other tools",
    )),
)

# 零宽字符与双向控制符(§8.3):常用于隐藏内容。
_HIDDEN_UNICODE = frozenset(
    chr(cp)
    for cp in (
        0x200B,
        0x200C,
        0x200D,
        0x2060,
        0xFEFF,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    )
)


def _is_obj_schema(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("type") == "object"


def _structure_findings(raw: Mapping[str, Any]) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        findings.append(AuditFinding("struct.name", "$.name", _MESSAGES["struct.name"]))
    elif len(name) > RAW_NAME_MAX:
        findings.append(
            AuditFinding("struct.name_too_long", "$.name", _MESSAGES["struct.name_too_long"])
        )
    if not _is_obj_schema(raw.get("inputSchema")):
        findings.append(
            AuditFinding(
                "struct.input_schema", "$.inputSchema", _MESSAGES["struct.input_schema"]
            )
        )
    if "outputSchema" in raw and raw["outputSchema"] is not None:
        if not _is_obj_schema(raw["outputSchema"]):
            findings.append(
                AuditFinding(
                    "struct.output_schema",
                    "$.outputSchema",
                    _MESSAGES["struct.output_schema"],
                )
            )
    return findings


def _serialized_findings(raw: Mapping[str, Any]) -> tuple[list[AuditFinding], bytes | None]:
    try:
        payload = json.dumps(raw, sort_keys=True, separators=_JSON_SEPARATORS, ensure_ascii=False)
    except (TypeError, ValueError):
        return [AuditFinding("struct.not_serializable", "$", _MESSAGES["struct.not_serializable"])], None
    encoded = payload.encode("utf-8")
    if len(encoded) > TOOL_DEF_MAX_BYTES:
        return [AuditFinding("struct.too_large", "$", _MESSAGES["struct.too_large"])], None
    return [], encoded


def _scan_text(text: str, path: str, findings: list[AuditFinding]) -> None:
    if any(ch in text for ch in _HIDDEN_UNICODE):
        findings.append(AuditFinding("inject.hidden_unicode", path, _MESSAGES["inject.hidden_unicode"]))
    lowered = text.lower()
    for rule, phrases in _INJECTION_RULES:
        if any(phrase in lowered for phrase in phrases):
            findings.append(AuditFinding(rule, path, _MESSAGES[rule]))


def _walk(node: Any, path: str, findings: list[AuditFinding]) -> None:
    if isinstance(node, str):
        if path.rsplit(".", 1)[-1] == "description" and len(node) > DESCRIPTION_MAX_CHARS:
            findings.append(
                AuditFinding(
                    "struct.description_too_long", path, _MESSAGES["struct.description_too_long"]
                )
            )
        _scan_text(node, path, findings)
    elif isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}"
            if isinstance(key, str):
                # 键命中时上报父路径:键文本本身可能是恶意/秘密内容,不回显。
                _scan_text(key, path, findings)
            _walk(value, child, findings)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _walk(value, f"{path}[{index}]", findings)


def _scan_findings(raw: Mapping[str, Any]) -> list[AuditFinding]:
    try:
        findings: list[AuditFinding] = []
        for key in ("name", "description"):
            value = raw.get(key)
            if isinstance(value, str):
                if key == "description" and len(value) > DESCRIPTION_MAX_CHARS:
                    findings.append(
                        AuditFinding(
                            "struct.description_too_long",
                            "$.description",
                            _MESSAGES["struct.description_too_long"],
                        )
                    )
                _scan_text(value, f"$.{key}", findings)
        for key in ("inputSchema", "outputSchema"):
            if key in raw and isinstance(raw[key], (Mapping, list, tuple)):
                _walk(raw[key], f"$.{key}", findings)
        return findings
    except Exception:
        return [AuditFinding("audit.scan_error", "$", _MESSAGES["audit.scan_error"])]


def _audit(raw: Mapping[str, Any]) -> tuple[AuditFinding, ...]:
    if not isinstance(raw, Mapping):
        return (AuditFinding("struct.invalid_type", "$", _MESSAGES["struct.invalid_type"]),)
    findings = _structure_findings(raw)
    serialized_findings, _ = _serialized_findings(raw)
    findings.extend(serialized_findings)
    if serialized_findings:
        return tuple(findings)
    findings.extend(_scan_findings(raw))
    return tuple(findings)


def audit_tool(raw: Mapping[str, Any]) -> tuple[AuditFinding, ...]:
    """审核单个原始工具定义;空 tuple 表示通过,任何异常都按拒绝处理。"""
    try:
        return _audit(raw)
    except Exception:
        return (AuditFinding("audit.scan_error", "$", _MESSAGES["audit.scan_error"]),)


def tool_fingerprint(raw: Mapping[str, Any]) -> str:
    """规范化 JSON(sort keys、紧凑分隔符、UTF-8)的 SHA-256,用于定义去重。

    原始定义不可 JSON 序列化时抛出 TypeError/ValueError;调用方应先通过审核。
    """
    payload = json.dumps(raw, sort_keys=True, separators=_JSON_SEPARATORS, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
