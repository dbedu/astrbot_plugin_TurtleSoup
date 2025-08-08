import asyncio
import os
import random
import re
from typing import Dict, List, Tuple

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter


@register("turtlesoup", "anchorAnc", "海龟汤互动解谜游戏，支持LLM自动出题和预设题库", "1.0.0")
class TurtleSoupPlugin(Star):
    """海龟汤互动解谜插件，支持预设题库和AI判断。"""
    # 消息模板
    MSG_GAME_IN_PROGRESS = "您已经有一个正在进行的海龟汤游戏了。如需继续，请直接提出您的问题。如需结束，请发送 /结束海龟汤。"
    MSG_DISCLAIMER = (
        "🐢 海龟汤推理游戏\n\n"
        "游戏规则：\n"
        "1. 我会给你一个看似不合理的情景\n"
        "2. 你只能提出能用'是'、'否'或'无关'回答的问题\n"
        "3. 通过这些问题推理出事情的真相\n"
        "4. 你有 {max_questions} 次提问机会，{session_timeout} 秒思考时间\n"
        "5. 提问格式: `/海龟汤提问 你的问题`\n\n"
        "现在开始推理吧！"
    )
    MSG_NO_PRESET_QUESTIONS = "题目库为空，无法开始游戏。"
    MSG_NO_AI_PROVIDER_FOR_JUDGE = "当前没有可用的AI服务，将使用简化判断模式。"
    MSG_AI_THINKING = "🤔 AI正在思考..."
    MSG_ROUND_RESULT = (
        "💭 第 {question_count} 问\n"
        "❓ {player_question}\n"
        "💡 {ai_answer}\n"
        "📊 剩余: {remaining_questions} 次\n"
    )
    MSG_CORRECT_ANSWER = (
        "🎉 恭喜答对了！\n\n"
        "完整答案：\n{answer}\n\n"
        "用了 {question_count} 次提问找到真相！\n"
        "使用 /开始海龟汤 可以挑战新题目。"
    )
    MSG_OUT_OF_QUESTIONS = (
        "🎯 游戏结束！\n\n"
        "用完了 {max_questions} 次提问机会。\n"
        "正确答案：\n{answer}\n\n"
        "使用 /开始海龟汤 开始新游戏。"
    )
    MSG_TIMEOUT = (
        "⏱️ 游戏超时！\n\n"
        "正确答案：\n{answer}\n\n"
        "使用 /开始海龟汤 开始新游戏。"
    )
    MSG_GAME_ENDED_BY_USER = (
        "👋 游戏结束\n\n"
        "正确答案：\n{answer}\n\n"
        "提问了 {question_count} 次。\n"
        "使用 /开始海龟汤 开始新游戏。"
    )
    MSG_GAME_FORCE_ENDED = "💥 海龟汤游戏已强制终止！使用 /开始海龟汤 开启新挑战。"
    MSG_NO_GAME_TO_END = "您当前没有正在进行的海龟汤游戏。"
    MSG_REVEAL_ANSWER = (
        "🎯 答案公布\n\n"
        "题目：{question}\n\n"
        "完整答案：\n{answer}\n\n"
        "已提问 {question_count} 次，可选择 /结束海龟汤。"
    )
    MSG_AI_CHECKING_ANSWER = "正在判断答案..."
    MSG_AI_ERROR = "AI暂时无法回应，请尝试 /强制结束海龟汤 重新开始。"
    MSG_UNKNOWN_ERROR = "游戏发生错误，已结束。"
    MSG_CHANGE_QUESTION = (
        "🔄 换题成功！\n\n"
        "新题目：\n{question}\n\n"
        "提问次数已重置，现在有 {max_questions} 次机会。"
    )

    def _get_session_key(self, event: AstrMessageEvent):
        """获取当前会话的唯一key，群聊为group_id，私聊为user_id。"""
        group_id = getattr(event, 'get_group_id', lambda: None)()
        if group_id:
            return group_id
        return event.get_sender_id()

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 优先使用配置文件参数，否则用默认值
        self.session_timeout = getattr(config, "session_timeout", 1000)
        self.max_questions = getattr(config, "max_questions", 40)
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.questions_file_path = os.path.join(plugin_dir, "questions_database.txt")
        self.questions_bank = self._parse_questions_bank()
        logger.info(f"题库初始化完成，共加载 {len(self.questions_bank)} 个题目")
        self.game_states: Dict[str, dict] = {}  # key: group_id or user_id

        # AI提示词配置
        self.hint_system_prompt = (
            "你是海龟汤游戏的出题人。你已经知道了完整的答案。玩家会向你提出问题，你必须严格按照以下规则回答：\n\n"
            "回答规则（严格遵守）：\n"
            "1. 只能回答以下五种答案之一：'是'、'否'、'无关'、'请重新提问'、'很接近了'\n"
            "2. 绝对不允许回答其他内容或添加解释\n"
            "3. 绝对不允许自己提出问题\n"
            "4. 绝对不允许透露答案的任何细节\n\n"
            "判断标准：\n"
            "- 如果玩家的问题答案是肯定的 → 回答'是'\n"
            "- 如果玩家的问题答案是否定的 → 回答'否'\n"
            "- 如果问题与故事核心无关 → 回答'无关'\n"
            "- 如果问题不清楚或无法理解 → 回答'请重新提问'\n"
            "- 如果玩家猜对了重要的关键信息，但还不是完整答案 → 回答'很接近了'\n\n"
            "当前题目：{question}\n答案：{answer}"
        )
        
        self.answer_check_prompt = (
            "请判断玩家的猜测是否正确。只能回答'是'或'否'，不要添加任何解释。\n\n"
            "正确答案：{answer}\n"
            "玩家猜测：{guess}\n\n"
            "判断标准：\n"
            "- 如果玩家猜测包含了答案的核心要点和关键细节，即使表述不完全一样 → 回答'是'\n"
            "- 如果玩家只是猜对了方向或大概内容，但缺少关键细节 → 回答'否'\n"
            "- 如果玩家猜测的核心内容完全错误 → 回答'否'\n\n"
            "只回答'是'或'否'，不要添加其他内容！"
        )

    def _parse_questions_bank(self) -> List[Tuple[str, str, dict]]:
        """从指定文件解析题目库"""
        questions = []
        try:
            with open(self.questions_file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                
            question_blocks = content.split('---')
            
            for i, block in enumerate(question_blocks):
                block = block.strip()
                if not block or block.startswith('#'):
                    continue
                    
                question_data = self._parse_question_block(block)
                if question_data:
                    questions.append(question_data)
                        
        except FileNotFoundError:
            logger.error(f"题库文件未找到: {self.questions_file_path}。将使用默认内置题目。")
            return self._get_default_questions()
        except Exception as e:
            logger.error(f"读取题库文件时发生错误: {e}。将使用默认内置题目。")
            return self._get_default_questions()

        if not questions:
            logger.warning(f"题库文件为空或格式不正确。将使用默认内置题目。")
            return self._get_default_questions()
        
        return questions
    
    def _parse_question_block(self, block: str) -> Tuple[str, str, dict]:
        """解析单个题目块"""
        lines = block.split('\n')
        question_info = {}
        
        for line in lines:
            line = line.strip()
            if ':' in line and not line.startswith('#'):
                key, value = line.split(':', 1)
                question_info[key.strip()] = value.strip()
        
        if 'ID' in question_info and '汤面' in question_info and '汤底' in question_info:
            try:
                metadata = {
                    'id': question_info.get('ID', ''),
                    'title': question_info.get('标题', ''),
                    'difficulty': int(question_info.get('难度', '3')),
                    'tags': [tag.strip() for tag in question_info.get('标签', '').split(',') if tag.strip()]
                }
                return (question_info['汤面'], question_info['汤底'], metadata)
            except (ValueError, KeyError) as e:
                logger.warning(f"解析题目出错: {e}")
                return None
        
        return None

    def _get_default_questions(self) -> List[Tuple[str, str, dict]]:
        """返回默认的内置题库"""
        return [
            ("一个男人推开门，看到眼前的景象后立即跳楼自杀了。为什么？", 
             "这个男人是灯塔管理员，他发现灯塔的灯灭了，意识到因为自己的疏忽导致船只失事，愧疚之下选择了跳楼。",
             {'id': '001', 'title': '灯塔看守员', 'difficulty': 3, 'tags': ['经典', '自杀', '责任']}),
            ("一个女人在餐厅点了一份海龟汤，喝了一口后就哭了。为什么？", 
             "这个女人曾经和丈夫一起遇难，丈夫告诉她煮的是海龟汤让她活下来，但她现在才知道当时喝的其实是丈夫的肉做的汤。",
             {'id': '002', 'title': '海龟汤', 'difficulty': 4, 'tags': ['经典', '食人', '背叛']})
        ]

    def _parse_ai_generated_content(self, content: str) -> Tuple[str, str]:
        """解析AI生成的题目内容"""
        try:
            story_match = re.search(r"故事：\s*(.*?)\s*答案：", content, re.DOTALL)
            answer_match = re.search(r"答案：\s*(.*)", content, re.DOTALL)

            if story_match and answer_match:
                question = story_match.group(1).strip()
                answer = answer_match.group(1).strip()
                if question and answer:
                    logger.debug("AI生成内容解析成功。")
                    return question, answer

            raise ValueError("解析AI生成内容失败")

        except Exception as e:
            logger.warning(f"AI生成内容解析失败: {e}")
            return "", ""

    @filter.command("开始海龟汤")
    async def start_turtle_soup(self, event: AstrMessageEvent):
        """
        开始一局海龟汤游戏。
        用法：/开始海龟汤 [题号]
        """
        user_id = event.get_sender_id()
        group_id = event.get_group_id()  # 获取群组ID，如果是私聊则为None

        session_key = self._get_session_key(event)

        if session_key in self.game_states:
            await event.send(MessageChain([Comp.Plain(self.MSG_GAME_IN_PROGRESS.format(user_id=user_id))]))
            return

        # 解析参数，检查是否指定了题号
        message_parts = event.message_str.split()
        specified_question_id = None
        
        if len(message_parts) > 1:
            try:
                # 尝试解析题号
                specified_question_id = message_parts[1].zfill(3)  # 补零到3位
            except (ValueError, IndexError):
                await event.send(MessageChain([Comp.Plain("题号格式错误，请使用数字。例如：/开始海龟汤 1")]))
                return

        await event.send(MessageChain([Comp.Plain(self.MSG_DISCLAIMER.format(
            max_questions=self.max_questions,
            session_timeout=self.session_timeout
        ))]))

        question, answer, metadata = self._get_question_and_answer(specified_question_id)
        if not question or not answer:
            if specified_question_id:
                await event.send(MessageChain([Comp.Plain(f"未找到题号 {specified_question_id} 的题目。使用 /题库列表 查看所有可用题目。")]))
            else:
                await event.send(MessageChain([Comp.Plain(self.MSG_NO_PRESET_QUESTIONS)]))
            return

        # 初始化游戏状态
        game_state = {
            "question": question,
            "answer": answer,
            "metadata": metadata,
            "question_count": 0,
            "llm_conversation_context": [],
            "controller": None, # 将用于存储会话控制器
        }
        self.game_states[session_key] = game_state
        logger.debug(f"为用户 {user_id} 创建了新的游戏状态。")

        # 构造题目介绍信息
        intro_text = f"📖 谜题 #{metadata['id']}"
        if metadata.get('title'):
            intro_text += f" - {metadata['title']}"
        
        difficulty_stars = "⭐" * metadata.get('difficulty', 3)
        intro_text += f" {difficulty_stars}\n\n"
        
        intro_text += f"{question}\n\n"
        intro_text += f"请使用 `/海龟汤提问 你的问题` 开始推理\n"
        intro_text += f"剩余提问次数：{self.max_questions}"

        await event.send(MessageChain([Comp.Plain(intro_text)]))

        llm_provider = self.context.get_using_provider()
        if not llm_provider:
            await event.send(MessageChain([Comp.Plain(self.MSG_NO_AI_PROVIDER_FOR_JUDGE)]))
        else:
            system_prompt = self.hint_system_prompt.format(question=question, answer=answer)
            game_state["llm_conversation_context"].append({"role": "system", "content": system_prompt})

        # 定义会话等待器
        @session_waiter(timeout=self.session_timeout, record_history_chains=False)
        async def turtle_soup_waiter(controller: SessionController, event: AstrMessageEvent):
            """游戏的主循环，处理玩家的每一次输入。"""
            # 首次交互时，存储会话控制器
            current_game_state = self.game_states.get(session_key)
            if current_game_state and not current_game_state.get("controller"):
                current_game_state["controller"] = controller
                logger.debug(f"为用户 {user_id} 的会话存储了 controller。")
            
            await self._handle_game_turn(event)

        try:
            logger.debug(f"用户 {user_id} 的海龟汤会话等待器已启动。")
            await turtle_soup_waiter(event)
        except asyncio.TimeoutError:
            logger.info(f"用户 {user_id} 的游戏会话超时。")
            answer = self.game_states.get(session_key, {}).get("answer", "未知")
            await event.send(MessageChain([Comp.Plain(self.MSG_TIMEOUT.format(answer=answer, user_id=user_id))]))
        except Exception as e:
            logger.error(f"海龟汤游戏会话发生未知错误: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain(self.MSG_UNKNOWN_ERROR.format(user_id=user_id))]))
        finally:
            logger.debug(f"用户 {user_id} 的会话等待器已结束，执行最终清理。")
            self._cleanup_game_session(session_key)
            event.stop_event()

    @filter.command("题库列表")
    async def list_questions(self, event: AstrMessageEvent):
        """
        显示题库中所有可用的题目列表。
        用法：/题库列表 [页数]
        """
        if not self.questions_bank:
            await event.send(MessageChain([Comp.Plain("题库为空，无法显示题目列表。")]))
            return
        
        # 解析页数参数
        message_parts = event.message_str.split()
        page = 1
        if len(message_parts) > 1:
            try:
                page = int(message_parts[1])
                if page < 1:
                    page = 1
            except ValueError:
                await event.send(MessageChain([Comp.Plain("页数格式错误，请使用数字。例如：/题库列表 2")]))
                return
        
        # 分页显示
        per_page = 10
        total_questions = len(self.questions_bank)
        total_pages = (total_questions + per_page - 1) // per_page
        
        if page > total_pages:
            page = total_pages
        
        start_idx = (page - 1) * per_page
        end_idx = min(start_idx + per_page, total_questions)
        
        result_text = f"📚 海龟汤题库 (第 {page}/{total_pages} 页)\n\n"
        
        for i in range(start_idx, end_idx):
            question, answer, metadata = self.questions_bank[i]
            difficulty_stars = "⭐" * metadata.get('difficulty', 3)
            title = metadata.get('title', '')
            question_id = metadata.get('id', str(i+1).zfill(3))
            
            result_text += f"#{question_id} {title} {difficulty_stars}\n"
            result_text += f"{question[:30]}{'...' if len(question) > 30 else ''}\n\n"
        
        result_text += f"使用 `/开始海龟汤 题号` 来选择特定题目"
        
        if total_pages > 1:
            result_text += f"\n使用 `/题库列表 页数` 查看其他页面"
        
        await event.send(MessageChain([Comp.Plain(result_text)]))
        event.stop_event()

    @filter.command("题目详情")
    async def question_detail(self, event: AstrMessageEvent):
        """
        显示指定题目的详细信息（不含答案）。
        用法：/题目详情 题号
        """
        message_parts = event.message_str.split()
        if len(message_parts) < 2:
            await event.send(MessageChain([Comp.Plain("请指定题号。例如：/题目详情 1")]))
            return
        
        try:
            question_id = message_parts[1].zfill(3)
        except ValueError:
            await event.send(MessageChain([Comp.Plain("题号格式错误，请使用数字。")]))
            return
        
        # 查找题目
        question, answer, metadata = self._get_question_and_answer(question_id)
        if not question:
            await event.send(MessageChain([Comp.Plain(f"未找到题号 {question_id} 的题目。")]))
            return
        
        difficulty_stars = "⭐" * metadata.get('difficulty', 3)
        
        detail_text = f"📖 题目详情 #{metadata.get('id', question_id)}\n\n"
        if metadata.get('title'):
            detail_text += f"标题: {metadata['title']}\n"
        detail_text += f"难度: {difficulty_stars}\n\n"
        detail_text += f"题目内容：\n{question}\n\n"
        detail_text += f"使用 `/开始海龟汤 {question_id}` 开始挑战这道题目"
        
        await event.send(MessageChain([Comp.Plain(detail_text)]))
        event.stop_event()

    async def _handle_game_turn(self, event: AstrMessageEvent):
        """处理游戏中的一个回合，包括命令和玩家提问。"""
        user_id = event.get_sender_id()
        player_input = event.message_str.strip()
        logger.debug(f"用户 {user_id} 的输入: '{player_input}'")

        session_key = self._get_session_key(event)

        # 检查游戏是否存在。这是最关键的检查点。
        game_state = self.game_states.get(session_key)
        if not game_state:
            logger.warning(f"处理回合时未找到用户 {user_id} 的游戏状态，可能已被清理。忽略此事件。")
            return

        # --- 命令处理 ---
        # 框架会自动移除命令前缀'/'，所以这里直接比较字符串
        # 检查是否是开始游戏的命令，以防止在游戏中误触
        if player_input.startswith('开始海龟汤'):
            await event.send(MessageChain([Comp.Plain(self.MSG_GAME_IN_PROGRESS)]))
            # 重置超时，因为用户有活动
            controller = game_state.get("controller")
            if controller:
                controller.keep(timeout=self.session_timeout, reset_timeout=True)
            return

        if player_input == '结束海龟汤':
            await self.end_turtle_soup(event)
            return
        if player_input == '强制结束海龟汤':
            await self.force_end_turtle_soup(event)
            return
        if player_input == '公布答案':
            await self.reveal_answer(event)
            return
        if player_input == '换一题':
            await self.change_question(event)
            return
        if player_input == '海龟汤帮助':
            await self._send_help_message(event)
            return
        if player_input.startswith('海龟汤提问'):
            message_parts = player_input.split(maxsplit=1)
            if len(message_parts) < 2 or not message_parts[1].strip():
                await event.send(MessageChain([Comp.Plain(
                    "❌ 问题内容为空\n\n"
                    "请使用正确格式：`/海龟汤提问 你的问题`\n\n"
                    "例如：`/海龟汤提问 他是故意的吗？`"
                )]))
                return
            
            question = message_parts[1].strip()
            await self._handle_turtle_soup_question(event, question)
            return
        if player_input == 'admin end turtle' and event.is_admin():
            await self._admin_end_all_games(event)
            return

        # 更新会话超时
        controller = game_state.get("controller")
        if not controller:
            logger.error(f"用户 {user_id} 的游戏状态中没有找到 controller！")
            self._cleanup_game_session(user_id)
            return
            
        controller.keep(timeout=self.session_timeout, reset_timeout=True)

        if not player_input:
            return

        # 如果用户输入的不是已定义的命令，直接忽略
        return

    def _get_question_and_answer(self, question_id: str = None) -> Tuple[str, str, dict]:
        """从题库中选择一个问题，支持指定题号。"""
        if not self.questions_bank:
            return None, None, {}
        
        if question_id:
            # 查找指定题号的题目
            for question, answer, metadata in self.questions_bank:
                if metadata.get('id') == question_id:
                    return question, answer, metadata
            return None, None, {}  # 未找到指定题目
        else:
            # 随机选择题目
            question, answer, metadata = random.choice(self.questions_bank)
            return question, answer, metadata

    def _cleanup_game_session(self, session_key: tuple):
        """清理指定用户的游戏会话和状态。"""
        game_state = self.game_states.pop(session_key, None)

        if game_state:
            controller = game_state.get("controller")
            if controller:
                controller.stop()
            logger.info(f"用户 {session_key} 的海龟汤游戏状态已清理。")

    async def _get_ai_judge_response(self, player_question: str, game_state: dict, session_id: str) -> str:
        """获取AI对玩家问题的判断（是/否/无关）。"""
        llm_provider = self.context.get_using_provider()
        if not llm_provider:
            return self._simple_judge(player_question, game_state["answer"])

        # 添加玩家问题到对话历史
        game_state["llm_conversation_context"].append({"role": "user", "content": player_question})

        # 调用LLM获取回答
        llm_response = await llm_provider.text_chat(
            prompt="",
            session_id=session_id,
            contexts=game_state["llm_conversation_context"],
        )
        ai_raw_answer = llm_response.completion_text.strip()
        
        # 验证并修正AI回答格式
        ai_answer = self._validate_ai_response(ai_raw_answer)
        
        # 添加修正后的AI回答到对话历史
        game_state["llm_conversation_context"].append({"role": "assistant", "content": ai_answer})
        
        return ai_answer

    def _validate_ai_response(self, ai_response: str) -> str:
        """验证并修正AI回答格式，确保只返回标准答案"""
        # 移除多余的空白字符
        response = ai_response.strip()
        
        # 允许的标准回答
        valid_responses = ['是', '否', '无关', '请重新提问', '很接近了', '你猜对了一部分']
        
        # 检查是否包含标准回答（优先匹配精确答案）
        for valid in valid_responses:
            if valid in response:
                return valid
        
        # 检查是否包含肯定/否定的关键词（注意顺序，先检查否定）
        if any(word in response for word in ['不对', '错误', '不是', '不']):
            return '否'
        elif any(word in response for word in ['对', '正确', '没错', '是的']):
            return '是'
        elif any(word in response for word in ['无关', '不相关', '没关系']):
            return '无关'
        
        # 如果都不匹配，默认返回"请重新提问"
        logger.warning(f"AI回答格式异常，原始回答: {response}")
        return '请重新提问'

    def _simple_judge(self, player_question: str, answer: str) -> str:
        """一个简化的判断逻辑，当没有LLM时使用。"""
        # 这是一个非常基础的实现，可以根据需要扩展
        if any(keyword in player_question for keyword in answer.split()):
            return "是"
        return "否"

    async def _is_answer_correct(self, player_guess: str, answer: str, session_id: str) -> bool:
        """使用LLM判断玩家是否猜对了答案。"""
        llm_provider = self.context.get_using_provider()
        if not llm_provider:
            # 如果没有LLM，使用改进的关键词匹配
            return self._simple_answer_check(player_guess, answer)

        try:
            prompt = self.answer_check_prompt.format(answer=answer, guess=player_guess)
            llm_response = await llm_provider.text_chat(
                prompt=prompt,
                session_id=session_id,
                contexts=[]
            )
            response_text = llm_response.completion_text.strip()
            logger.debug(f"答案检查LLM响应: '{response_text}'")
            return "是" in response_text
        except Exception as e:
            logger.error(f"使用LLM检查答案时出错: {e}")
            # 发生错误时，使用改进的关键词匹配
            return self._simple_answer_check(player_guess, answer)

    def _simple_answer_check(self, player_guess: str, answer: str) -> bool:
        """改进的简单答案检查，当没有LLM时使用"""
        # 将答案和猜测都转换为小写进行比较
        guess_lower = player_guess.lower()
        answer_lower = answer.lower()
        
        # 提取答案中的关键词（去除常见的连接词）
        stop_words = {'的', '了', '是', '在', '和', '与', '或', '但', '然后', '因为', '所以', '这', '那', '一个', '就', '也', '都'}
        
        # 简单的关键词提取
        answer_words = set()
        for word in answer_lower:
            if len(word) > 1 and word not in stop_words:
                answer_words.add(word)
        
        # 检查猜测中是否包含答案的关键概念
        # 这里可以根据具体需求调整匹配度
        match_count = 0
        total_key_words = len(answer_words)
        
        for word in answer_words:
            if word in guess_lower:
                match_count += 1
        
        # 如果匹配的关键词达到一定比例，认为是正确的
        if total_key_words > 0:
            match_ratio = match_count / total_key_words
            return match_ratio >= 0.5  # 提高到50%的关键词匹配才认为正确
        
        return False

    @filter.command("结束海龟汤")
    async def cmd_end_turtle_soup(self, event: AstrMessageEvent):
        """命令：结束当前用户的海龟汤游戏。"""
        await self.end_turtle_soup(event)
        event.stop_event()

    @filter.command("强制结束海龟汤")
    async def cmd_force_end_turtle_soup(self, event: AstrMessageEvent):
        """命令：强制结束当前用户的海龟汤游戏。"""
        await self.force_end_turtle_soup(event)
        event.stop_event()

    @filter.command("公布答案")
    async def cmd_reveal_answer(self, event: AstrMessageEvent):
        """命令：在游戏中提前查看答案。"""
        await self.reveal_answer(event)
        event.stop_event()

    @filter.command("换一题")
    async def cmd_change_question(self, event: AstrMessageEvent):
        """命令：在游戏中更换题目。"""
        await self.change_question(event)
        event.stop_event()

    @filter.command("海龟汤提问")
    async def cmd_turtle_soup_question(self, event: AstrMessageEvent):
        """命令：在游戏中提问。用法：/海龟汤提问 你的问题"""
        user_id = event.get_sender_id()
        
        # 检查是否有正在进行的游戏
        session_key = self._get_session_key(event)
        if session_key not in self.game_states:
            await event.send(MessageChain([Comp.Plain("❌ 没有正在进行的游戏，请先使用 `/开始海龟汤` 开始游戏。")]))
            event.stop_event()
            return
            
        # 解析问题内容
        message_parts = event.message_str.split(maxsplit=1)
        if len(message_parts) < 2 or not message_parts[1].strip():
            await event.send(MessageChain([Comp.Plain(
                "❌ 问题内容为空\n\n"
                "请使用正确格式：`/海龟汤提问 你的问题`\n\n"
                "例如：`/海龟汤提问 他是故意的吗？`"
            )]))
            event.stop_event()
            return
            
        question = message_parts[1].strip()
        
        # 处理游戏提问
        await self._handle_turtle_soup_question(event, question)
        event.stop_event()

    async def _handle_turtle_soup_question(self, event: AstrMessageEvent, question: str):
        """处理海龟汤游戏中的提问"""
        user_id = event.get_sender_id()
        session_key = self._get_session_key(event)
        game_state = self.game_states.get(session_key)

        if not game_state:
            await event.send(MessageChain([Comp.Plain("❌ 游戏状态异常，请重新开始游戏。")]))
            return
            
        controller = game_state.get("controller")
        if controller:
            controller.keep(timeout=self.session_timeout, reset_timeout=True)
        
        game_state["question_count"] += 1

        # 判断是否是猜测答案
        # 更精确地判断是否为猜测答案：需要包含明确的推理或断言
        guess_keywords = ["答案是", "真相是", "因为", "所以", "是因为", "原因是", "我觉得是", "我认为是", "应该是", "一定是", "肯定是"]
        is_a_guess = (any(keyword in question for keyword in guess_keywords) or 
                     (len(question) > 25 and any(word in question for word in ["导致", "造成", "结果", "发生了", "事实是"])) or
                     ("是" in question and len(question) > 15 and any(word in question for word in ["死", "杀", "害", "做", "发生"])))
        if is_a_guess:
            await event.send(MessageChain([Comp.Plain(self.MSG_AI_CHECKING_ANSWER)]))
            
            is_correct = await self._is_answer_correct(question, game_state["answer"], event.get_session_id())
            
            # 再次检查游戏状态，防止在AI判断期间游戏被结束
            if session_key not in self.game_states:
                return

            if is_correct:
                metadata = game_state.get("metadata", {})
                correct_text = f"🎉 恭喜答对了！\n\n"
                correct_text += f"完整答案：\n{game_state['answer']}\n\n"
                correct_text += f"用了 {game_state['question_count']} 次提问找到真相！\n"
                
                # 游戏结束后显示标签
                if metadata.get('tags'):
                    correct_text += f"🏷️ 标签: {', '.join(metadata['tags'])}\n"
                
                correct_text += f"使用 /开始海龟汤 挑战新题目。"
                
                await event.send(MessageChain([Comp.Plain(correct_text)]))
                self._cleanup_game_session(session_key)
                return
        
        # 检查是否超出提问次数
        if game_state["question_count"] > self.max_questions:
            metadata = game_state.get("metadata", {})
            timeout_text = f"🎯 游戏结束！\n\n"
            timeout_text += f"你已经用完了 {self.max_questions} 次提问机会。\n\n"
            timeout_text += f"正确答案是：\n{game_state['answer']}\n\n"
            
            # 显示标签
            if metadata.get('tags'):
                timeout_text += f"🏷️ 标签: {', '.join(metadata['tags'])}\n"
            
            timeout_text += f"感谢参与！使用 /开始海龟汤 可以开始新游戏。"
            
            await event.send(MessageChain([Comp.Plain(timeout_text)]))
            self._cleanup_game_session(session_key)
            return

        # 调用AI进行判断
        #await event.send(MessageChain([Comp.Plain(self.MSG_AI_THINKING)]))
        
        try:
            ai_answer = await self._get_ai_judge_response(question, game_state, event.get_session_id())
            
            # 再次检查，防止在AI响应期间游戏被终止
            if session_key not in self.game_states:
                return

            remaining_questions = self.max_questions - game_state["question_count"]
            await event.send(MessageChain([Comp.Plain(self.MSG_ROUND_RESULT.format(
                question_count=game_state["question_count"],
                player_question=question,
                ai_answer=ai_answer,
                remaining_questions=remaining_questions
            ))]))
            
        except Exception as e:
            logger.error(f"AI响应时发生错误: {e}")
            await event.send(MessageChain([Comp.Plain(self.MSG_AI_ERROR)]))
            return

    async def end_turtle_soup(self, event: AstrMessageEvent):
        """正常结束当前用户的海龟汤游戏。"""
        user_id = event.get_sender_id()
        session_key = self._get_session_key(event)
        game_state = self.game_states.get(session_key)

        if game_state:
            answer = game_state.get("answer", "未知")
            question_count = game_state.get("question_count", 0)
            metadata = game_state.get("metadata", {})
            
            end_text = f"👋 游戏结束 👋\n\n"
            end_text += f"你主动结束了游戏。\n\n"
            end_text += f"正确答案是：\n{answer}\n\n"
            
            # 显示标签
            if metadata.get('tags'):
                end_text += f"🏷️ 标签: {', '.join(metadata['tags'])}\n"
            
            end_text += f"你在结束前共提问了 {question_count} 次。\n"
            end_text += f"感谢参与！使用 /开始海龟汤 可以开始新游戏。"
            
            await event.send(MessageChain([Comp.Plain(end_text)]))
            
            self._cleanup_game_session(session_key)
        else:
            await event.send(MessageChain([Comp.Plain(self.MSG_NO_GAME_TO_END)]))

    async def force_end_turtle_soup(self, event: AstrMessageEvent):
        """强制结束当前用户的海龟汤游戏。"""
        user_id = event.get_sender_id()
        session_key = self._get_session_key(event)
        if session_key in self.game_states:
            self._cleanup_game_session(session_key)
            await event.send(MessageChain([Comp.Plain(self.MSG_GAME_FORCE_ENDED)]))
        else:
            await event.send(MessageChain([Comp.Plain(self.MSG_NO_GAME_TO_END)]))

    async def reveal_answer(self, event: AstrMessageEvent):
        """在游戏中提前查看答案。"""
        user_id = event.get_sender_id()
        session_key = self._get_session_key(event)
        if session_key in self.game_states:
            game_state = self.game_states[session_key]
            metadata = game_state.get("metadata", {})
            
            reveal_text = f"🎯 答案公布 🎯\n\n"
            reveal_text += f"📖 题目 #{metadata.get('id', 'Unknown')}"
            if metadata.get('title'):
                reveal_text += f" - {metadata['title']}"
            reveal_text += f"\n\n题目：{game_state['question']}\n\n"
            reveal_text += f"完整答案：\n{game_state['answer']}\n\n"
            reveal_text += f"你已经提问了 {game_state['question_count']} 次。\n"
            reveal_text += f"游戏将继续进行，您也可以选择 /结束海龟汤。"
            
            await event.send(MessageChain([Comp.Plain(reveal_text)]))
        else:
            await event.send(MessageChain([Comp.Plain(self.MSG_NO_GAME_TO_END)]))

    async def change_question(self, event: AstrMessageEvent):
        """在游戏中更换题目。"""
        user_id = event.get_sender_id()
        session_key = self._get_session_key(event)
        game_state = self.game_states.get(session_key)

        if not game_state:
            await event.send(MessageChain([Comp.Plain(self.MSG_NO_GAME_TO_END)]))
            return
            
        # 获取新题目，确保与当前题目不同
        current_question = game_state["question"]
        max_attempts = 10  # 最多尝试10次避免无限循环
        attempts = 0
        
        while attempts < max_attempts:
            new_question, new_answer, new_metadata = self._get_question_and_answer()
            if new_question and new_answer and new_question != current_question:
                break
            attempts += 1
        
        if not new_question or not new_answer:
            await event.send(MessageChain([Comp.Plain("抱歉，无法获取新题目。请稍后再试。")]))
            return
            
        # 更新游戏状态
        game_state["question"] = new_question
        game_state["answer"] = new_answer
        game_state["metadata"] = new_metadata
        game_state["question_count"] = 0  # 重置提问次数
        game_state["llm_conversation_context"] = []  # 清空对话历史
        
        # 重新设置LLM上下文
        llm_provider = self.context.get_using_provider()
        if llm_provider:
            system_prompt = self.hint_system_prompt.format(question=new_question, answer=new_answer)
            game_state["llm_conversation_context"].append({"role": "system", "content": system_prompt})
        
        # 重置会话超时
        controller = game_state.get("controller")
        if controller:
            controller.keep(timeout=self.session_timeout, reset_timeout=True)
            
        # 构造新题目介绍信息
        change_text = f"🔄 换题成功！\n\n"
        change_text += f"📖 新题目 #{new_metadata['id']}"
        if new_metadata.get('title'):
            change_text += f" - {new_metadata['title']}"
        
        difficulty_stars = "⭐" * new_metadata.get('difficulty', 3)
        change_text += f"\n🌟 难度: {difficulty_stars}\n\n"
        
        change_text += f"题目：\n{new_question}\n\n"
        change_text += f"提问次数已重置，你现在有 {self.max_questions} 次新的提问机会。\n"
        change_text += f"请开始你的推理！"
            
        await event.send(MessageChain([Comp.Plain(change_text)]))
        
        logger.info(f"用户 {user_id} 成功更换题目：{new_question[:50]}...")

    async def _admin_end_all_games(self, event: AstrMessageEvent):
        """强制结束所有游戏的核心逻辑。"""
        if not self.game_states:
            await event.send(MessageChain([Comp.Plain("当前没有活跃的海龟汤游戏。")]))
            return

        stopped_count = len(self.game_states)
        # 创建一个副本进行迭代，因为 _cleanup_game_session 会修改字典
        for session_key in list(self.game_states.keys()):
            self._cleanup_game_session(session_key)

        await event.send(MessageChain([Comp.Plain(
            f"✅ 管理员操作完成。\n"
            f"已强制终止所有 {stopped_count} 个活跃的海龟汤游戏。"
        )]))
        logger.info(f"管理员强制结束了所有 {stopped_count} 个海龟汤游戏。")

    @filter.command("admin end turtle")
    async def cmd_admin_end_all_turtle_games(self, event: AstrMessageEvent):
        """
        管理员命令：立即强制结束所有在线的海龟汤游戏。
        """
        if not event.is_admin():
            await event.send(MessageChain([Comp.Plain("❌ 权限不足，只有管理员可操作此命令。")]))
            event.stop_event()
            return
        
        await self._admin_end_all_games(event)
        event.stop_event()

    async def _send_help_message(self, event: AstrMessageEvent):
        """发送帮助信息。"""
        help_message = (
            "🐢 海龟汤推理游戏 - 帮助手册 🐢\n\n"
            "欢迎来到由AI驱动的海龟汤推理世界！\n\n"
            "基本指令:\n"
            "  - `/开始海龟汤`：随机开始一局新游戏\n"
            "  - `/开始海龟汤 题号`：选择特定题目开始游戏\n"
            "  - `/海龟汤提问 你的问题`：在游戏中提问\n"
            "  - `/结束海龟汤`：主动结束当前游戏并查看答案\n"
            "  - `/强制结束海龟汤`：立即强制结束当前游戏\n"
            "  - `/公布答案`：在不结束游戏的情况下查看答案\n"
            "  - `/换一题`：更换当前题目，提问次数重置\n\n"
            "题库指令:\n"
            "  - `/题库列表`：查看所有可用题目\n"
            "  - `/题库列表 页数`：查看指定页的题目列表\n"
            "  - `/题目详情 题号`：查看指定题目的详细信息\n\n"
            "管理员指令:\n"
            "  - `/admin end turtle`：强制结束所有正在进行的游戏\n\n"
            "💡 游戏玩法:\n"
            "  - 游戏开始后，系统会给出一个看似不合理的情景\n"
            "  - 你的任务是提出可以用'是'、'否'或'无关'回答的问题\n"
            "  - 提问方式: 使用 `/海龟汤提问 你的问题` 格式\n"
            "  - 当你觉得已经知道真相时，可以用 `/海龟汤提问 答案是...` 格式说出答案\n"
            f"  - 每局游戏有 {self.max_questions} 次提问机会和 {self.session_timeout} 秒思考时间\n\n"
            "🎯 题目选择:\n"
            "  - 题目按难度分为 1-5 星级（⭐-⭐⭐⭐⭐⭐）\n"
            "  - 可以通过题号直接选择喜欢的题目\n"
            "  - 每个题目都有独特的标题便于识别\n\n"
            "祝您推理愉快！🕵️‍♀️"
        )
        await event.send(MessageChain([Comp.Plain(help_message)]))

    @filter.command("海龟汤帮助")
    async def turtle_soup_help(self, event: AstrMessageEvent):
        """
        显示海龟汤推理游戏插件的所有可用命令。
        """
        await self._send_help_message(event)
        event.stop_event()

    async def terminate(self):
        """插件终止时调用，用于清理所有活跃的游戏会话。"""
        logger.info("正在终止 TurtleSoupPlugin 并清理所有活跃的游戏会话...")
        if self.game_states:
            for session_key in list(self.game_states.keys()):
                self._cleanup_game_session(session_key)
            logger.info("所有活跃的海龟汤游戏会话已被终止。")
        logger.info("TurtleSoupPlugin terminated。")
