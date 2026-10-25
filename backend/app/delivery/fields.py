"""Public field contract shared by selection, metrics and file encoders."""

COMPANY_FIELDS = ["company_id", "company_name", "domain", "website_url", "website_binding_status", "website_binding_evidence", "country_code", "region", "city", "address",
                  "industry_text", "industry_codes", "description", "brands", "certifications", "products", "knows_about", "company_type", "employee_count", "revenue", "linkedin_url", "branch_id", "contact_form_url", "social_urls", "main_email", "main_phone", "source_count",
                  "email_count", "phone_count", "named_contact_count", "contactable", "qualification_score", "confidence", "lead_score", "score_breakdown", "status", "missing", "conflicts", "last_verified_at"]
CONTACT_FIELDS = ["contact_id", "company_id", "contact_name", "title", "email", "email_type", "email_status",
                  "syntax_status", "mx_status", "catch_all_status", "smtp_status", "disposable_status", "is_role_account", "phone", "phone_raw", "phone_extension", "phone_type", "phone_status",
                  "line_type", "binding_status", "contact_priority", "priority_score", "source_urls", "retrieved_at", "last_verified_at", "confidence", "confidence_components", "tags", "inferred", "opt_out"]
SOURCE_FIELDS = ["entity_id", "field", "value", "source_type", "source_url", "retrieved_at", "source_excerpt", "method", "confidence"]
EMAIL_CHANNEL_FIELDS = ("email", "email_type", "email_status", "syntax_status", "mx_status", "catch_all_status", "smtp_status", "disposable_status", "is_role_account")
HEADERS_ZH = {"company_id": "企业ID", "company_name": "企业名称", "domain": "域名", "website_url": "官网", "country_code": "国家代码",
              "website_binding_status": "官网归属状态", "website_binding_evidence": "官网归属证据", "source_excerpt": "来源原文摘录",
              "region": "州省地区", "city": "城市", "address": "地址", "industry_text": "行业", "company_type": "企业类型",
              "employee_count": "员工数量", "main_email": "企业邮箱", "main_phone": "企业电话", "source_count": "来源数量",
              "industry_codes": "行业代码", "revenue": "营业收入", "linkedin_url": "公开职业网址", "branch_id": "分支ID",
              "description": "企业简介", "brands": "品牌", "certifications": "认证", "products": "公开产品", "knows_about": "公开业务主题",
              "contact_form_url": "联系表单网址", "social_urls": "公开社交网址", "catch_all_status": "全收邮箱检查", "smtp_status": "SMTP检查",
              "disposable_status": "一次性邮箱检查", "is_role_account": "是否角色邮箱",
              "email_count": "邮箱数量", "phone_count": "电话数量", "named_contact_count": "具名联系人数", "contactable": "是否有联系渠道",
              "confidence": "置信度", "status": "状态", "missing": "缺失项", "conflicts": "冲突项", "last_verified_at": "最近校验时间",
              "qualification_score": "资格证据置信度", "lead_score": "线索优先分", "score_breakdown": "评分明细", "contact_priority": "联系渠道优先级", "priority_score": "联系渠道优先分",
              "confidence_components": "置信度因素",
              "contact_id": "联系人ID", "contact_name": "联系人姓名", "title": "职位", "email": "邮箱", "email_type": "邮箱类型",
              "email_status": "邮箱状态", "syntax_status": "语法状态", "mx_status": "MX状态", "phone": "电话", "phone_raw": "原始电话",
              "phone_extension": "分机", "phone_type": "电话类型", "phone_status": "电话状态", "line_type": "线路类型", "binding_status": "绑定状态",
              "source_urls": "来源网址", "retrieved_at": "采集时间", "tags": "标签", "inferred": "是否推测", "opt_out": "退出联系",
              "entity_id": "实体ID", "field": "字段", "value": "值", "source_type": "来源类型", "source_url": "来源网址", "method": "提取方法",
              "form_fields": "表单字段证据"}
