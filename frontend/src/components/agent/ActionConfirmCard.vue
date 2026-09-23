<script setup lang="ts">
/**
 * Human-in-the-loop 确认卡片。
 *
 * ★ 这是整个项目最核心的 UI 组件。
 *
 * 设计目标：让用户在【数据被修改之前】能真正核对清楚，而不是
 * 盲目点"同意"。具体做了四件事：
 *
 *   ① 展示【具体工单列表】，不是"17 条"这种数字
 *      —— 数字没法核对，具体列表才能发现"这条不该改"
 *   ② 支持逐条取消勾选，并把结果回传给后端
 *   ③ 明确倒计时，避免用户以为点了就永久等待
 *   ④ "拒绝"按钮同等显眼，不做诱导确认的暗黑模式
 */

import { computed, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import type { PendingAction, PreviewItem } from '@/types/agent'
import { PRIORITY_LABELS, PRIORITY_TAG_TYPE, type TicketPriority } from '@/types/ticket'

const props = defineProps<{
  action: PendingAction
  /** 是否正在执行中 */
  executing?: boolean
  /** 执行结果（执行完后显示） */
  progress?: { succeeded: number; failed: number; total: number } | null
}>()

const emit = defineEmits<{
  /** 用户点了确认，带上勾选后要执行的工单 ID 列表 */
  (e: 'approve', selectedIds: number[]): void
  /** 用户点了拒绝，带上原因（选填） */
  (e: 'reject', note: string): void
}>()

/** 被取消勾选的工单号集合 */
const unselected = ref<Set<string>>(new Set())

/** 剩余秒数 */
const remaining = ref(0)
let timer: ReturnType<typeof setInterval> | null = null

/** 预览里每条工单需要有稳定的 key，用工单号 */
function itemKey(item: PreviewItem): string {
  return item.ticket_no ?? Math.random().toString(36)
}

/** 勾选状态 */
const selectedCount = computed(
  () => props.action.preview.length - unselected.value.size,
)

const allSelected = computed(() => unselected.value.size === 0)

/** 已确认执行完 */
const finished = computed(() => !!props.progress && !props.executing)

const expired = computed(() => remaining.value <= 0 && !props.executing && !finished.value)

function toggleItem(key: string): void {
  if (props.executing || finished.value || expired.value) return
  const next = new Set(unselected.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  unselected.value = next
}

function toggleAll(): void {
  if (props.executing || finished.value || expired.value) return
  unselected.value = allSelected.value
    ? new Set(props.action.preview.map(itemKey))
    : new Set()
}

/** 倒计时格式化 */
const countdown = computed(() => {
  const s = Math.max(0, remaining.value)
  const m = Math.floor(s / 60)
  const sec = s % 60
  return `${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
})

function startCountdown(): void {
  if (timer) clearInterval(timer)

  const deadline = new Date(props.action.expiresAt).getTime()
  const tick = () => {
    remaining.value = Math.max(0, Math.floor((deadline - Date.now()) / 1000))
    if (remaining.value <= 0 && timer) {
      clearInterval(timer)
      timer = null
    }
  }
  tick()
  timer = setInterval(tick, 1000)
}

// 换了一个 action 就重新计时（比如用户又发起了一次批量操作）
watch(
  () => props.action.actionId,
  () => {
    unselected.value = new Set()
    startCountdown()
  },
  { immediate: true },
)

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

async function handleApprove(): Promise<void> {
  const ids = props.action.preview
    .filter((item) => !unselected.value.has(itemKey(item)))
    .map((item) => (item as any).id)
    .filter((id): id is number => typeof id === 'number')

  if (ids.length === 0) {
    ElMessage.warning('至少要选择一条工单')
    return
  }

  // 二次确认：数量不一致时特别提醒，避免误操作
  if (ids.length !== props.action.affectedCount) {
    try {
      await ElMessageBox.confirm(
        `你取消了 ${props.action.affectedCount - ids.length} 条工单，` +
          `实际将修改 ${ids.length} 条。确定继续吗？`,
        '确认范围已调整',
        { confirmButtonText: '继续执行', cancelButtonText: '再想想', type: 'warning' },
      )
    } catch {
      return
    }
  }

  emit('approve', ids)
}

async function handleReject(): Promise<void> {
  let note = ''
  try {
    const result = await ElMessageBox.prompt('确定取消这次操作吗？', '取消操作', {
      confirmButtonText: '确认取消',
      cancelButtonText: '返回',
      inputPlaceholder: '取消原因（选填）',
      inputValue: '',
      type: 'warning',
    })
    note = result.value ?? ''
  } catch {
    return
  }
  emit('reject', note)
}
</script>

<template>
  <div class="confirm-card" :class="{ expired, finished }">
    <!-- ── 头部 ─────────────────────────────────────── -->
    <div class="card-head">
      <div class="head-left">
        <el-icon class="warn-icon"><WarningFilled /></el-icon>
        <span class="head-title">
          {{ finished ? '执行完成' : expired ? '已超时失效' : '需要你确认的操作' }}
        </span>
      </div>

      <div v-if="!finished && !expired" class="countdown">
        <el-icon><Timer /></el-icon>
        剩余 {{ countdown }}
      </div>
    </div>

    <!-- ── 计划描述 ─────────────────────────────────── -->
    <div class="plan-title">{{ action.title }}</div>

    <!-- ── 执行进度（执行中/已完成时显示）─────────────── -->
    <div v-if="progress" class="progress-box">
      <div class="progress-stats">
        <span class="stat-ok">成功 {{ progress.succeeded }}</span>
        <span v-if="progress.failed > 0" class="stat-fail">失败 {{ progress.failed }}</span>
        <span class="stat-total">共 {{ progress.total }}</span>
      </div>
      <el-progress
        :percentage="
          progress.total > 0
            ? Math.round(((progress.succeeded + progress.failed) / progress.total) * 100)
            : 0
        "
        :status="progress.failed > 0 ? 'warning' : 'success'"
      />
    </div>

    <!-- ── 工单列表 ─────────────────────────────────── -->
    <div v-else class="list-head">
      <span>影响的工单（共 {{ action.affectedCount }} 条，已选 {{ selectedCount }} 条）</span>
      <el-button link type="primary" size="small" @click="toggleAll">
        {{ allSelected ? '全部取消' : '全部选择' }}
      </el-button>
    </div>

    <div v-if="!progress" class="ticket-list">
      <div
        v-for="item in action.preview"
        :key="itemKey(item)"
        class="ticket-item"
        :class="{ unselected: unselected.has(itemKey(item)) }"
        @click="toggleItem(itemKey(item))"
      >
        <el-checkbox
          :model-value="!unselected.has(itemKey(item))"
          :disabled="executing || expired"
          @click.stop
          @change="toggleItem(itemKey(item))"
        />

        <span class="ticket-no">{{ item.ticket_no }}</span>
        <span class="ticket-title">{{ item.title }}</span>

        <el-tag
          v-if="item.priority"
          :type="PRIORITY_TAG_TYPE[item.priority as TicketPriority] as any"
          size="small"
          effect="plain"
        >
          {{ PRIORITY_LABELS[item.priority as TicketPriority] }}
        </el-tag>
      </div>

      <div v-if="action.preview.length === 0" class="empty-hint">
        没有可预览的工单明细
      </div>
    </div>

    <!-- ── 操作按钮 ─────────────────────────────────── -->
    <div v-if="!finished && !expired" class="card-actions">
      <el-button :disabled="executing" @click="handleReject">拒绝</el-button>
      <el-button
        type="primary"
        :loading="executing"
        :disabled="expired"
        @click="handleApprove"
      >
        {{ executing ? '执行中...' : `确认执行（${selectedCount} 条）` }}
      </el-button>
    </div>

    <div v-if="expired" class="expired-hint">
      这个确认已超过 {{ Math.round(action.timeoutSeconds / 60) }} 分钟有效期，
      请重新发起操作。
    </div>
  </div>
</template>

<style scoped>
.confirm-card {
  border: 1px solid #f0c78a;
  background: #fffbf5;
  border-radius: 8px;
  padding: 12px;
  margin: 8px 0;
}

.confirm-card.finished {
  border-color: #b3e19d;
  background: #f0f9eb;
}

.confirm-card.expired {
  border-color: #dcdfe6;
  background: #f5f7fa;
  opacity: 0.75;
}

/* 头部 */
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.head-left {
  display: flex;
  align-items: center;
  gap: 6px;
}

.warn-icon {
  color: #e6a23c;
  font-size: 16px;
}

.head-title {
  font-size: 13px;
  font-weight: 600;
  color: #b88230;
}

.countdown {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: #e6a23c;
  font-family: 'Consolas', monospace;
}

/* 计划标题 */
.plan-title {
  font-size: 13px;
  color: #303133;
  line-height: 1.6;
  margin-bottom: 10px;
  padding: 8px;
  background: #fff;
  border-radius: 4px;
}

/* 进度 */
.progress-box {
  margin-bottom: 10px;
}

.progress-stats {
  display: flex;
  gap: 12px;
  font-size: 12px;
  margin-bottom: 6px;
}

.stat-ok {
  color: #67c23a;
}

.stat-fail {
  color: #f56c6c;
}

.stat-total {
  color: #909399;
}

/* 列表 */
.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: #909399;
  margin-bottom: 6px;
}

.ticket-list {
  max-height: 220px;
  overflow-y: auto;
  border: 1px solid #f0e0c8;
  border-radius: 4px;
  background: #fff;
  margin-bottom: 10px;
}

.ticket-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-bottom: 1px solid #faf5eb;
  cursor: pointer;
  font-size: 12px;
  transition: background 0.15s;
}

.ticket-item:last-child {
  border-bottom: none;
}

.ticket-item:hover {
  background: #fffbf5;
}

.ticket-item.unselected {
  opacity: 0.45;
  text-decoration: line-through;
}

.ticket-no {
  font-family: 'Consolas', monospace;
  color: #606266;
  flex-shrink: 0;
}

.ticket-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #303133;
}

.empty-hint {
  padding: 16px;
  text-align: center;
  color: #c0c4cc;
  font-size: 12px;
}

/* 按钮 */
.card-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.expired-hint {
  font-size: 12px;
  color: #909399;
  text-align: center;
  padding: 4px 0;
}
</style>
