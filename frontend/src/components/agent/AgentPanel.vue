<script setup lang="ts">
/**
 * Copilot 对话面板。
 *
 * 结构：头部（会话管理）→ 消息列表 → 输入区
 * 悬浮在页面右下角，点按钮展开成抽屉。
 */

import { nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { useAgentStore } from '@/stores/agent'
import AgentStepTimeline from '@/components/agent/AgentStepTimeline.vue'
import ActionConfirmCard from '@/components/agent/ActionConfirmCard.vue'

const store = useAgentStore()
const router = useRouter()

const input = ref('')
const messageListRef = ref<HTMLDivElement>()

/** 快捷提问 —— 降低首次使用门槛，也让 Demo 演示更快 */
const SUGGESTIONS = [
  { label: '查高优先级工单', text: '帮我看看最近有哪些高优先级工单没处理' },
  { label: '分析 SLA 风险', text: '分析最近 7 天的 SLA 风险' },
  { label: '报个故障', text: '线上支付接口大量出现 502，帮我报个故障' },
  {
    label: '批量升级分派',
    text: '把所有超过 24 小时未处理的工单升级成高优先级，并分配给技术部门',
  },
]

/** 自动滚到底部。新消息和流式输出时都要滚 */
async function scrollToBottom(): Promise<void> {
  await nextTick()
  const el = messageListRef.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(
  () => store.messages.length,
  scrollToBottom,
)

// 流式输出时内容不断变长，也要跟着滚
watch(
  () => store.lastMessage?.content,
  scrollToBottom,
)

watch(
  () => store.lastMessage?.steps.length,
  scrollToBottom,
)

async function handleSend(): Promise<void> {
  const text = input.value.trim()
  if (!text || store.busy) return
  input.value = ''
  await store.send(text)
}

function useSuggestion(text: string): void {
  if (store.busy) return
  input.value = text
  handleSend()
}

async function handleApprove(actionId: string, ids: number[]): Promise<void> {
  await store.approve(actionId, ids)
  // 批量操作会影响工单数据，执行完刷新一下当前页面
  if (router.currentRoute.value.name === 'tickets') {
    router.go(0)
  }
}

async function handleReject(actionId: string, note: string): Promise<void> {
  await store.reject(actionId, note || null)
}

function openTicket(ticketNo: string): void {
  // 引用工单可以点击跳转 —— 这是"防幻觉"设计的一部分：
  // 用户能一眼核对 AI 说的工单是不是真的存在
  const route = router.resolve({
    name: 'tickets',
    query: { keyword: ticketNo },
  })
  window.open(route.href, '_blank')
}
</script>

<template>
  <div>
    <!-- ── 悬浮按钮 ─────────────────────────────────── -->
    <div v-show="!store.visible" class="fab" @click="store.open()">
      <el-icon :size="22"><ChatDotRound /></el-icon>
      <span class="fab-text">AI 助手</span>
      <span v-if="store.awaitingApproval" class="fab-dot"></span>
    </div>

    <!-- ── 抽屉面板 ─────────────────────────────────── -->
    <el-drawer
      v-model="store.visible"
      :with-header="false"
      size="560px"
      class="agent-drawer"
      :modal="false"
    >
      <div class="panel">
        <!-- 头部 -->
        <div class="panel-head">
          <div class="head-left">
            <el-icon class="head-icon"><MagicStick /></el-icon>
            <span class="head-title">OpsPilot Copilot</span>
          </div>
          <div class="head-actions">
            <el-button link size="small" @click="store.newConversation()">
              <el-icon><Plus /></el-icon>
              新对话
            </el-button>
            <el-button link size="small" @click="store.toggle()">
              <el-icon><Close /></el-icon>
            </el-button>
          </div>
        </div>

        <!-- 消息列表 -->
        <div ref="messageListRef" class="messages">
          <!-- 空状态 -->
          <div v-if="!store.hasMessages" class="empty">
            <el-icon :size="40" class="empty-icon"><MagicStick /></el-icon>
            <div class="empty-title">我是 OpsPilot 智能助手</div>
            <div class="empty-desc">
              可以用自然语言让我创建工单、查询工单、分析 SLA 风险，<br />
              或者批量处理工单（批量操作会先让你确认）
            </div>

            <div class="suggestions">
              <div
                v-for="s in SUGGESTIONS"
                :key="s.label"
                class="suggestion"
                @click="useSuggestion(s.text)"
              >
                {{ s.label }}
              </div>
            </div>
          </div>

          <!-- 消息 -->
          <div
            v-for="msg in store.messages"
            :key="msg.id"
            class="message"
            :class="msg.role"
          >
            <!-- 执行过程（只在助手消息上方显示） -->
            <AgentStepTimeline
              v-if="msg.role === 'assistant' && msg.steps.length > 0"
              :steps="msg.steps"
              :streaming="msg.streaming"
            />

            <!-- 正文 -->
            <div v-if="msg.content" class="bubble">
              {{ msg.content }}
              <span v-if="msg.streaming" class="cursor">▋</span>
            </div>

            <!-- 等待确认时的加载提示 -->
            <div
              v-else-if="msg.streaming && !msg.pendingAction"
              class="bubble thinking"
            >
              正在处理...
            </div>

            <!-- ★ 确认卡片 -->
            <ActionConfirmCard
              v-if="msg.pendingAction"
              :action="msg.pendingAction"
              :executing="store.streaming"
              :progress="store.actionProgress"
              @approve="(ids) => handleApprove(msg.pendingAction!.actionId, ids)"
              @reject="(note) => handleReject(msg.pendingAction!.actionId, note)"
            />

            <!-- 引用工单 -->
            <div v-if="msg.citations && msg.citations.length > 0" class="citations">
              <span class="citations-label">引用的工单：</span>
              <el-tag
                v-for="c in msg.citations.slice(0, 8)"
                :key="c.ticket_no"
                size="small"
                class="citation-tag"
                @click="openTicket(c.ticket_no)"
              >
                {{ c.ticket_no }}
              </el-tag>
              <span v-if="msg.citations.length > 8" class="dim">
                等 {{ msg.citations.length }} 条
              </span>
            </div>
          </div>
        </div>

        <!-- 输入区 -->
        <div class="input-area">
          <el-input
            v-model="input"
            type="textarea"
            :rows="2"
            :disabled="store.awaitingApproval"
            :placeholder="
              store.awaitingApproval
                ? '请先在上方确认或拒绝待执行的操作'
                : '用自然语言描述你的需求，Enter 发送，Shift+Enter 换行'
            "
            resize="none"
            @keydown.enter.exact.prevent="handleSend"
          />

          <div class="input-actions">
            <span class="input-hint">
              <template v-if="store.awaitingApproval">
                <el-icon><WarningFilled /></el-icon>
                等待你确认
              </template>
            </span>

            <el-button
              v-if="store.streaming"
              size="small"
              @click="store.abort()"
            >
              停止
            </el-button>
            <el-button
              type="primary"
              size="small"
              :disabled="!input.trim() || store.busy"
              :loading="store.streaming"
              @click="handleSend"
            >
              发送
            </el-button>
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
/* ── 悬浮按钮 ─────────────────────────────────────── */
.fab {
  position: fixed;
  right: 28px;
  bottom: 28px;
  z-index: 2000;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 18px;
  background: linear-gradient(135deg, #409eff 0%, #2d5a87 100%);
  color: #fff;
  border-radius: 26px;
  cursor: pointer;
  box-shadow: 0 6px 20px rgba(64, 158, 255, 0.4);
  transition: transform 0.2s, box-shadow 0.2s;
  user-select: none;
}

.fab:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 26px rgba(64, 158, 255, 0.5);
}

.fab-text {
  font-size: 14px;
  font-weight: 500;
}

/* 有待确认操作时的小红点 */
.fab-dot {
  position: absolute;
  top: 6px;
  right: 10px;
  width: 9px;
  height: 9px;
  background: #f56c6c;
  border-radius: 50%;
  animation: pulse 1.4s infinite;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.6;
    transform: scale(1.25);
  }
}

/* ── 面板 ─────────────────────────────────────────── */
.panel {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 0 12px;
  border-bottom: 1px solid #ebeef5;
}

.head-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.head-icon {
  color: #409eff;
  font-size: 18px;
}

.head-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
}

/* ── 消息区 ───────────────────────────────────────── */
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 14px 0;
}

.empty {
  text-align: center;
  padding: 40px 20px;
}

.empty-icon {
  color: #c6e2ff;
  margin-bottom: 12px;
}

.empty-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 8px;
}

.empty-desc {
  font-size: 13px;
  color: #909399;
  line-height: 1.7;
  margin-bottom: 24px;
}

.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
}

.suggestion {
  padding: 6px 14px;
  border: 1px solid #d9ecff;
  background: #f4f9ff;
  color: #409eff;
  border-radius: 16px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}

.suggestion:hover {
  background: #409eff;
  color: #fff;
  border-color: #409eff;
}

/* 消息 */
.message {
  margin-bottom: 16px;
}

.message.user {
  display: flex;
  justify-content: flex-end;
}

.message.user .bubble {
  background: #409eff;
  color: #fff;
  max-width: 80%;
}

.message.assistant .bubble {
  background: #f4f4f5;
  color: #303133;
}

.bubble {
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 14px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  display: inline-block;
  max-width: 100%;
}

.bubble.thinking {
  color: #909399;
  font-style: italic;
}

/* 打字机光标 */
.cursor {
  animation: blink 1s step-end infinite;
  color: #409eff;
}

@keyframes blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0;
  }
}

/* 引用工单 */
.citations {
  margin-top: 8px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
}

.citations-label {
  font-size: 12px;
  color: #909399;
}

.citation-tag {
  cursor: pointer;
}

.citation-tag:hover {
  background: #409eff;
  color: #fff;
}

.dim {
  font-size: 12px;
  color: #c0c4cc;
}

/* ── 输入区 ───────────────────────────────────────── */
.input-area {
  border-top: 1px solid #ebeef5;
  padding-top: 12px;
}

.input-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
}

.input-hint {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: #e6a23c;
}
</style>
