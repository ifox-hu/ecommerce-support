import asyncio
import json
import re
from time import perf_counter

from httpx import AsyncClient, TimeoutException
from openai import APITimeoutError

from app.services.retrieval import PRODUCT_SPEC_TERMS, UNCOVERED, topics_for


INTENTS = {
    "policy": "咨询商城规则、退换货条件、运费、发票、质保或具体商品资料",
    "order": "查询已有订单或支付状态",
    "logistics": "查询配送、快递、物流或上次订单到哪里",
    "aftersale": "查询已发起的退款、售后或退货进展",
    "handoff": "明确要求人工、申请售后或发起投诉",
    "complex": "多轮纠纷、破损且重复投诉或同时涉及多个复杂约束",
    "general": "有明确内容的日常问答、常识、写作",
    "professional": "商城以外需要资料核验的专业判断、法规、规范、精确数据或时效性事实",
    "clarify": "消息含糊或缺少判断意图所需的信息",
}
ORDER_PATTERN = re.compile(r"(?<![A-Z0-9])ORD-\d{4,12}(?![A-Z0-9])", re.I)
VAGUE_OPENING = re.compile(
    r"(?:[\s，。！？：,.!?:]*(?:(?:客服)?你好|您好|在吗|有人吗|能帮我吗|有个问题|我?想(?:咨询|问)"
    r"(?:一下|件事|个问题|个事|点事)?|请帮忙确认|麻烦看看|麻烦了|谢谢(?:帮忙)?|多谢|请确认一下))"
    r"+[\s，。！？：,.!?:]*"
)
PROFESSIONAL_GUIDANCE = (
    (
        ("处方", "药物", "用药", "布洛芬", "治疗", "诊断", "症状", "副作用", "剂量", "病历"),
        "如果需要确认用药或药物能否合用，请带上完整药名、剂量和其他在用药物的信息，向开药医生或药师核对。",
    ),
    (
        ("法律", "违法", "起诉", "判决", "法规", "劳动合同", "竞业限制", "劳动仲裁", "合同效力", "赔偿责任"),
        "判断具体情况时，需要核对适用地区、相关日期和文件原文。可以带上这些材料向当地法律援助机构或律师核实。",
    ),
    (
        ("投资", "理财", "股票", "基金", "收益率", "报税", "财务审计"),
        "可先核对相关机构发布的正式资料。涉及个人投资、纳税或财务决定时，请把具体条件交给相应的专业人士核实。",
    ),
)
PROFESSIONAL_TERMS = (
    "行业标准", "国家标准", "认证标准", "技术规范", "标准号", "国标", "适用条款", "监管要求", "科研论文",
    "实时数据", "最新数据", "汇率",
)
GENERAL_PROMPT = (
    "你是智能客服的通用问答助手。按用户的语言直接、简明地回答当前问题。"
    "解释概念时用标准术语，先给核心结论，再用一两句话解释原理。写作请求直接给正文。"
    "你只可提供通用知识。遇到缺少可核验依据的专业判断、时效性事实或具体产品数据，"
    "先说明缺少资料，再提供可帮助用户理解的通用信息，避免做具体诊断、用药、法律或投资决定。"
    "不要编造引用、商城政策、商品参数、订单事实或实时信息，不承诺代用户办理业务。"
)


def extract_order(text: str) -> str | None:
    match = ORDER_PATTERN.search(text)
    return match.group().upper() if match else None


def forced_intent(text: str) -> str | None:
    if any(word in text for word in ("转人工", "人工客服", "找人工", "人工处理", "真人客服", "找真人")):
        return "handoff"
    if any(word in text for word in ("破损", "损坏", "碎了", "少件")) and any(
            word in text for word in ("多次", "反复", "催单", "一直没", "投诉", "拒绝")):
        return "complex"
    return None


def professional_guidance(text: str) -> str | None:
    return next((advice for terms, advice in PROFESSIONAL_GUIDANCE if any(term in text for term in terms)), None)


def demo_intent(text: str) -> str:
    if any(word in text for word in ("退款进", "退款到账了", "退款状态", "退到哪", "售后进", "售后状态", "退货进", "退回的钱", "退款结果")):
        return "aftersale"
    if topics_for(text):
        return "policy"
    if any(word in text for word in ("物流", "快递", "配送", "运单", "到哪", "到哪里", "什么时候送", "运输", "签收了吗", "发货了吗")):
        if extract_order(text) or not re.search(r"是什么意思|是什么|怎么用|怎么读|怎么说|怎么写|什么区别|工作原理|英文", text):
            return "logistics"
    if any(word in text for word in ("投诉", "建单", "申请退款", "申请售后", "申请退货")):
        return "handoff"
    if extract_order(text) or any(word in text for word in ("查订单", "查一下订单", "订单状态", "查询订单", "订单信息", "支付状态", "付款情况",
                                                            "那笔订单", "刚才的订单", "我的订单", "上一笔订单", "再查一下")):
        return "order"
    if any(word in text for word in UNCOVERED + (
        "商城", "平台政策", "店铺规则", "商家规定", "优惠券", "会员权益", "商品参数",
        "你们这款", "你家这款", "该商品", "这款商品", "这款产品", "支持分期", "价格保护",
    )):
        return "policy"
    if not text.strip(" \t\r\n，。！？,.!?") or VAGUE_OPENING.fullmatch(text.strip()):
        return "clarify"
    if professional_guidance(text) or any(term in text for term in PROFESSIONAL_TERMS):
        return "professional"
    return "general"


class DecisionService:
    def __init__(self, settings, observability, http: AsyncClient):
        self.settings = settings
        self.observability = observability
        self.http = http
        self.chat_model = None
        if settings.mode == "full":
            from langchain_openai import ChatOpenAI

            self.chat_model = ChatOpenAI(model=settings.chat_model, api_key=settings.chat_api_key,
                                         base_url=settings.chat_base_url, timeout=18, max_retries=0,
                                         temperature=0)

    def _jev(self, state: str, questions: dict):
        from typesafe_sdk import TypeSafeClient

        with TypeSafeClient(api_key=self.settings.jev_api_key, model=self.settings.jev_model, timeout=9) as client:
            return client.system_one(state=state, questions=questions)

    async def _measure(self, name, operation):
        started = perf_counter()
        outcome = "error"
        try:
            with self.observability.span(f"model.{name}", model=name):
                result = await operation()
            outcome = "ok"
            return result
        except (TimeoutError, TimeoutException, APITimeoutError):
            outcome = "timeout"
            raise
        finally:
            self.observability.observe("model", name, outcome, perf_counter() - started)

    async def classify(self, text: str, pending: str | None) -> dict[str, float]:
        if self.chat_model is None:
            selected = pending if pending and extract_order(text) else demo_intent(text)
            return {intent: 0.94 if intent == selected else 0.01 for intent in INTENTS}

        from typesafe_sdk import Choice

        async def decide():
            result = await asyncio.wait_for(asyncio.to_thread(self._jev, text, {
                "intent": Choice(instructions="分类顾客消息。仅按定义选择一类。", criteria=INTENTS),
            }), 12)
            return result.choices["intent"].probabilities

        return await self._measure("jev_choice", decide)

    async def evidence(self, question: str, candidates: list[dict]) -> tuple[list[dict], float, dict]:
        if not candidates:
            return [], 0.01, {}
        if self.chat_model is None:
            topics = topics_for(question)
            constraints = {term for term in UNCOVERED + PRODUCT_SPEC_TERMS if term in question}
            relevance = {source["id"]: 0.96 if source["topic"] in topics and
                         (not constraints or constraints.intersection(source.get("covers", []))) else 0.04
                         for source in candidates}
            selected = [source for source in candidates if relevance[source["id"]] >= 0.70]
            covered = topics and topics.issubset({source["topic"] for source in selected})
            conditions = {condition for source in selected for condition in source.get("covers", [])}
            sufficient = 0.94 if covered and constraints.issubset(conditions) else 0.08
            return selected, sufficient, relevance

        from typesafe_sdk import Noul

        async def decide():
            state = json.dumps({"question": question, "candidate_clauses": candidates}, ensure_ascii=False)
            questions = {source["id"]: Noul(instructions=f"条款 {source['id']} 是否对回答顾客问题有实质相关性？")
                         for source in candidates}
            result = await asyncio.wait_for(asyncio.to_thread(self._jev, state, questions), 12)
            relevance = {item_id: answer.noul for item_id, answer in result.nouls.items()}
            selected = [source for source in candidates if relevance.get(source["id"], 0) >= 0.70]
            if not selected:
                return [], 0.01, relevance
            state = json.dumps({"question": question, "relevant_clauses": selected}, ensure_ascii=False)
            result = await asyncio.wait_for(asyncio.to_thread(self._jev, state, {
                "enough": Noul(instructions="这些适用的现行条款是否完整覆盖顾客问题的所有具体条件，且不存在版本或渠道冲突？"),
            }), 12)
            return selected, result.nouls["enough"].noul, relevance

        return await self._measure("jev_noul", decide)

    async def answer_general(self, question: str, previous_turns: list[dict], *, run_id: str, session_id: str) -> str:
        recent = previous_turns[-4:]
        if self.chat_model is not None:
            payload = {"question": question, "previous_turns": recent}
            return await self.completion("general_chat", GENERAL_PROMPT, payload, run_id=run_id, session_id=session_id)

        async def complete():
            response = await self.http.post(
                self.settings.general_model_url.rstrip("/") + "/api/chat",
                json={
                    "model": self.settings.general_model,
                    "messages": [
                        {"role": "system", "content": GENERAL_PROMPT},
                        *recent,
                        {"role": "user", "content": question},
                    ],
                    "stream": False,
                    "think": False,
                    "keep_alive": "15m",
                    "options": {"num_ctx": 3072, "num_predict": 1024, "temperature": 0.2},
                },
                timeout=80,
            )
            response.raise_for_status()
            result = response.json()
            content = result["message"]["content"].strip()
            if not content:
                raise ValueError("Model returned no text")
            if tokens := result.get("prompt_eval_count", 0) + result.get("eval_count", 0):
                self.observability.observe("model_usage", "general_chat", "ok", tokens=tokens)
            return content

        return await self._measure("general_chat", complete)

    async def completion(self, name: str, system: str, payload: dict, *, run_id="", session_id="") -> str:
        async def complete():
            with self.observability.langchain_span(run_id=run_id, session_id=session_id) as config:
                response = await self.chat_model.ainvoke(
                    [("system", system), ("human", json.dumps(payload, ensure_ascii=False))], config=config,
                )
            usage = getattr(response, "usage_metadata", None) or {}
            if count := int(usage.get("total_tokens", 0)):
                self.observability.observe("model_usage", name, "ok", tokens=count)
            if not isinstance(response.content, str) or not response.content.strip():
                raise ValueError("Model returned no text")
            return response.content.strip()

        return await self._measure(name, complete)
