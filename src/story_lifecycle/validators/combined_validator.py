"""Combined validator — occupation-email linkage rules."""

from __future__ import annotations


OCCUPATION_RULES: dict[str, dict] = {
    "civil_servant": {
        "required_suffixes": [".gov.cn"],
        "error_code": "EMAIL_SUFFIX_NOT_MATCH_CIVIL_SERVANT",
        "message": "公务员应使用 .gov.cn 邮箱",
    },
    "student": {
        "required_suffixes": [".edu.cn", ".edu"],
        "error_code": "EMAIL_SUFFIX_NOT_MATCH_STUDENT",
        "message": "学生应使用 .edu.cn 或 .edu 邮箱",
    },
}


class CombinedValidator:
    def validate(
        self,
        email: str,
        occupation: str = "",
    ) -> tuple[bool, str, str]:
        """Validate occupation-email linkage.

        Returns (ok, error_code, detail).
        """
        if not occupation or not email:
            return True, "", ""

        rule = OCCUPATION_RULES.get(occupation)
        if not rule:
            return True, "", ""

        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if not any(domain.endswith(s) for s in rule["required_suffixes"]):
            return False, rule["error_code"], rule["message"]

        return True, "", ""
