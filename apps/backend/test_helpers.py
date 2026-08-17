"""Anonymous test fixtures generated entirely in memory."""

import io
import zipfile
from xml.sax.saxutils import escape


ANONYMOUS_RESUME_TEXT = """
匿名候选人
求职方向：Python 后端工程师
个人概况：5 年企业应用开发经验，负责接口设计、性能优化和线上稳定性建设。
核心技能：Python、Flask、FastAPI、PostgreSQL、Redis、Docker、Linux、Git、CI/CD。
工作经历：2021 年至今，在某科技公司担任后端工程师，负责订单、用户和数据服务。
项目一：重构核心 API，通过缓存和索引优化将平均响应时间降低 35%，接口可用性达到 99.9%。
项目二：建设自动化发布流水线，将人工发布时间从 40 分钟缩短到 10 分钟，回滚时间控制在 5 分钟内。
项目三：参与容器化迁移，维护 Docker 镜像和部署文档，支持测试与生产环境一致性。
项目四：设计权限与审计模块，覆盖角色、资源和操作日志，配合安全团队完成上线验收。
工程实践：编写单元测试、接口文档和运行手册，持续跟踪告警、容量与技术债治理。
协作经验：与产品、前端、测试和运维团队协作，参与需求评审、技术方案设计和故障复盘。
教育经历：本科，计算机科学与技术专业，2020 年毕业。
证书能力：英语六级，持续学习云原生、系统设计和数据工程相关知识。
职业目标：希望在重视工程质量和业务价值的团队中承担后端系统建设工作。
""".strip()


def build_anonymous_resume_docx() -> bytes:
    paragraphs = "".join(
        "<w:p><w:r><w:t>" + escape(line) + "</w:t></w:r></w:p>"
        for line in ANONYMOUS_RESUME_TEXT.splitlines()
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs}</w:body></w:document>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document.encode("utf-8"))
    return output.getvalue()
