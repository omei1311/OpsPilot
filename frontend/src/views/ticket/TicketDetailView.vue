<script setup lang="ts">
/**
 * 工单详情页。
 *
 * 三块内容：基本信息 + 操作区（改状态/分派）+ 评论与操作记录。
 */

import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import * as ticketApi from '@/api/ticket'
import {
  CATEGORY_LABELS,
  EVENT_LABELS,
  PRIORITY_LABELS,
  STATUS_LABELS,
  type TicketDetail,
  type TicketEvent,
  type TicketPriority,
  type TicketStatus,
} from '@/types/ticket'
import TicketTags from '@/components/ticket/TicketTags.vue'

const route = useRoute()
const router = useRouter()

const ticketId = Number(route.params.id)

const loading = ref(false)
const ticket = ref<TicketDetail | null>(null)

const statusOptions = Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }))
const priorityOptions = Object.entries(PRIORITY_LABELS).map(([value, label]) => ({ value, label }))

/**
 * ★ 状态机的前端副本。
 *
 * 和后端 TicketService.ALLOWED_TRANSITIONS 保持一致。
 *
 * 为什么前后端都要有一份？
 *   后端那份是【权威】—— 决定请求能不能通过，不可绕过。
 *   前端这份是【体验】—— 让下拉框只显示合法的目标状态，
 *   用户不会选到一个必然报错的选项。
 *
 * 这是"前端校验为了体验，后端校验为了安全"的典型场景。
 * 就算前端被绕过，后端那层依然拦得住。
 */
const ALLOWED_TRANSITIONS: Record<TicketStatus, TicketStatus[]> = {
  pending: ['processing', 'closed'],
  processing: ['waiting', 'resolved', 'pending'],
  waiting: ['processing', 'closed'],
  resolved: ['closed', 'processing'],
  closed: [],
}

const allowedNextStatuses = computed(() => {
  if (!ticket.value) return []
  const allowed = ALLOWED_TRANSITIONS[ticket.value.status] ?? []
  return statusOptions.filter((op) => allowed.includes(op.value as TicketStatus))
})

// ── 加载 ──────────────────────────────────────────────────
async function fetchDetail(): Promise<void> {
  loading.value = true
  try {
    ticket.value = await ticketApi.getTicket(ticketId)
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

// ── 状态流转 ──────────────────────────────────────────────
const statusDialogVisible = ref(false)
const statusForm = reactive({ status: '' as TicketStatus | '', reason: '' })
const changingStatus = ref(false)

function openStatusDialog(): void {
  statusForm.status = ''
  statusForm.reason = ''
  statusDialogVisible.value = true
}

async function submitStatusChange(): Promise<void> {
  if (!statusForm.status) {
    ElMessage.warning('请选择目标状态')
    return
  }
  changingStatus.value = true
  try {
    await ticketApi.changeTicketStatus(ticketId, statusForm.status, statusForm.reason || undefined)
    ElMessage.success('状态已更新')
    statusDialogVisible.value = false
    await fetchDetail()
  } catch {
    // 后端会返回具体的非法流转原因，拦截器已弹出
  } finally {
    changingStatus.value = false
  }
}

// ── 修改优先级 ────────────────────────────────────────────
async function handlePriorityChange(value: TicketPriority): Promise<void> {
  try {
    await ticketApi.updateTicket(ticketId, { priority: value })
    ElMessage.success('优先级已更新，SLA 截止时间已重新计算')
    await fetchDetail()
  } catch {
    // 拦截器已提示
  }
}

// ── 分派 ──────────────────────────────────────────────────
const assignDialogVisible = ref(false)
const assignForm = reactive({ assignee_id: undefined as number | undefined })
const assigning = ref(false)

/** 可选的处理人列表。真实项目会有专门的用户列表接口，这里从环境拿一个简单的 */
const assigneeCandidates = ref<{ id: number; username: string }[]>([])

async function openAssignDialog(): Promise<void> {
  assignForm.assignee_id = ticket.value?.assignee_id ?? undefined
  assignDialogVisible.value = true
  // 延迟加载候选人：只有真要用的时候才请求
  if (assigneeCandidates.value.length === 0) {
    try {
      // 复用统计接口拿到的处理人，避免为此再加一个用户列表接口
      const { getWorkload } = await import('@/api/dashboard')
      const list = await getWorkload(50)
      assigneeCandidates.value = list.map((w) => ({ id: w.user_id, username: w.username }))
    } catch {
      assigneeCandidates.value = []
    }
  }
}

async function submitAssign(): Promise<void> {
  if (!assignForm.assignee_id) {
    ElMessage.warning('请选择负责人')
    return
  }
  assigning.value = true
  try {
    await ticketApi.assignTicket(ticketId, { assignee_id: assignForm.assignee_id })
    ElMessage.success('分派成功')
    assignDialogVisible.value = false
    await fetchDetail()
  } catch {
    // 拦截器已提示
  } finally {
    assigning.value = false
  }
}

// ── 评论 ──────────────────────────────────────────────────
const commentText = ref('')
const submittingComment = ref(false)

async function submitComment(): Promise<void> {
  const content = commentText.value.trim()
  if (!content) {
    ElMessage.warning('请输入评论内容')
    return
  }
  submittingComment.value = true
  try {
    await ticketApi.addComment(ticketId, content)
    commentText.value = ''
    ElMessage.success('评论已发表')
    await fetchDetail()
  } catch {
    // 拦截器已提示
  } finally {
    submittingComment.value = false
  }
}

// ── 展示工具 ──────────────────────────────────────────────
function formatTime(value: string | null): string {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** 把操作记录的 event_data 渲染成一句人话 */
function describeEvent(event: TicketEvent): string {
  const d = (event.event_data ?? {}) as Record<string, unknown>

  switch (event.event_type) {
    case 'created':
      return `创建了工单 ${d.ticket_no ?? ''}`
    case 'status_changed':
      return `状态从「${STATUS_LABELS[d.from as TicketStatus] ?? d.from}」变更为「${STATUS_LABELS[d.to as TicketStatus] ?? d.to}」${d.reason ? `，原因：${d.reason}` : ''}`
    case 'priority_changed':
      return `优先级从「${PRIORITY_LABELS[d.from as TicketPriority] ?? d.from}」调整为「${PRIORITY_LABELS[d.to as TicketPriority] ?? d.to}」`
    case 'assigned':
      return d.to_assignee_id ? `分派给用户 #${d.to_assignee_id}` : '变更了负责部门'
    case 'updated':
      return `修改了字段：${Array.isArray(d.fields) ? d.fields.join('、') : ''}`
    case 'commented':
      return `发表了评论：${d.preview ?? ''}`
    default:
      return EVENT_LABELS[event.event_type] ?? event.event_type
  }
}

onMounted(fetchDetail)
</script>

<template>
  <div v-loading="loading" class="detail-page">
    <el-page-header class="page-header" @back="router.back()">
      <template #content>
        <span v-if="ticket">{{ ticket.ticket_no }}</span>
        <span v-else>工单详情</span>
      </template>
    </el-page-header>

    <template v-if="ticket">
      <!-- ── 基本信息 ─────────────────────────────────── -->
      <el-card shadow="never" class="card">
        <template #header>
          <div class="card-header">
            <span class="title">{{ ticket.title }}</span>
            <TicketTags
              :status="ticket.status"
              :priority="ticket.priority"
              :sla-status="ticket.sla_status"
              :sla-remaining="ticket.sla_remaining_minutes"
            />
          </div>
        </template>

        <el-descriptions :column="3" border>
          <el-descriptions-item label="分类">
            {{ CATEGORY_LABELS[ticket.category] }}
          </el-descriptions-item>
          <el-descriptions-item label="创建人">
            {{ ticket.creator_name ?? '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="负责人">
            <span v-if="ticket.assignee_name">{{ ticket.assignee_name }}</span>
            <span v-else class="muted">未分派</span>
          </el-descriptions-item>

          <el-descriptions-item label="负责部门">
            {{ ticket.department_name ?? '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="创建时间">
            {{ formatTime(ticket.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="SLA 截止">
            {{ formatTime(ticket.sla_deadline) }}
          </el-descriptions-item>

          <el-descriptions-item label="解决时间">
            {{ formatTime(ticket.resolved_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="更新时间">
            {{ formatTime(ticket.updated_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="工单 ID">
            #{{ ticket.id }}
          </el-descriptions-item>
        </el-descriptions>

        <div v-if="ticket.description" class="description">
          <div class="section-label">详细描述</div>
          <div class="description-text">{{ ticket.description }}</div>
        </div>
      </el-card>

      <!-- ── 操作区 ───────────────────────────────────── -->
      <el-card shadow="never" class="card">
        <template #header><span class="card-title">操作</span></template>

        <div class="actions">
          <div class="action-item">
            <span class="action-label">变更状态</span>
            <el-button
              v-if="allowedNextStatuses.length > 0"
              type="primary"
              plain
              @click="openStatusDialog"
            >
              变更状态
            </el-button>
            <el-tag v-else type="info" size="small">已是终态，不可再变更</el-tag>
          </div>

          <div class="action-item">
            <span class="action-label">调整优先级</span>
            <el-select
              :model-value="ticket.priority"
              style="width: 140px"
              @change="handlePriorityChange"
            >
              <el-option
                v-for="op in priorityOptions"
                :key="op.value"
                :label="op.label"
                :value="op.value"
              />
            </el-select>
            <span class="action-hint">修改后会重新计算 SLA 截止时间</span>
          </div>

          <div class="action-item">
            <span class="action-label">负责处理</span>
            <el-button type="primary" plain @click="openAssignDialog">
              {{ ticket.assignee_id ? '转派' : '分派' }}
            </el-button>
          </div>
        </div>
      </el-card>

      <!-- ── 评论 + 操作记录 ──────────────────────────── -->
      <el-row :gutter="16">
        <el-col :span="14">
          <el-card shadow="never" class="card">
            <template #header>
              <span class="card-title">评论（{{ ticket.comments.length }}）</span>
            </template>

            <div class="comment-list">
              <el-empty v-if="ticket.comments.length === 0" description="还没有评论" :image-size="60" />
              <div v-for="c in ticket.comments" :key="c.id" class="comment">
                <div class="comment-head">
                  <span class="comment-author">{{ c.author_name ?? '未知用户' }}</span>
                  <span class="comment-time">{{ formatTime(c.created_at) }}</span>
                </div>
                <div class="comment-body">{{ c.content }}</div>
              </div>
            </div>

            <div class="comment-input">
              <el-input
                v-model="commentText"
                type="textarea"
                :rows="3"
                placeholder="输入评论内容，Ctrl+Enter 快速提交"
                maxlength="5000"
                @keydown.ctrl.enter="submitComment"
              />
              <el-button
                type="primary"
                class="comment-submit"
                :loading="submittingComment"
                @click="submitComment"
              >
                发表评论
              </el-button>
            </div>
          </el-card>
        </el-col>

        <el-col :span="10">
          <el-card shadow="never" class="card">
            <template #header>
              <span class="card-title">操作记录（{{ ticket.events.length }}）</span>
            </template>

            <el-timeline class="timeline">
              <el-timeline-item
                v-for="e in ticket.events"
                :key="e.id"
                :timestamp="formatTime(e.created_at)"
                placement="top"
                size="normal"
              >
                <div class="event-text">
                  <span class="event-actor">{{ e.actor_name ?? '系统' }}</span>
                  {{ describeEvent(e) }}
                </div>
              </el-timeline-item>
            </el-timeline>
          </el-card>
        </el-col>
      </el-row>
    </template>

    <!-- ── 状态变更弹窗 ───────────────────────────────── -->
    <el-dialog v-model="statusDialogVisible" title="变更工单状态" width="460px">
      <el-form label-width="80px">
        <el-form-item label="目标状态">
          <el-select v-model="statusForm.status" placeholder="请选择" style="width: 100%">
            <el-option
              v-for="op in allowedNextStatuses"
              :key="op.value"
              :label="op.label"
              :value="op.value"
            />
          </el-select>
          <div class="form-hint">
            只列出当前状态下允许流转的目标状态（状态机限制）
          </div>
        </el-form-item>
        <el-form-item label="变更原因">
          <el-input
            v-model="statusForm.reason"
            type="textarea"
            :rows="2"
            placeholder="选填"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="statusDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="changingStatus" @click="submitStatusChange">
          确认变更
        </el-button>
      </template>
    </el-dialog>

    <!-- ── 分派弹窗 ───────────────────────────────────── -->
    <el-dialog v-model="assignDialogVisible" title="分派工单" width="420px">
      <el-form label-width="80px">
        <el-form-item label="负责人">
          <el-select
            v-model="assignForm.assignee_id"
            placeholder="请选择处理人"
            filterable
            style="width: 100%"
          >
            <el-option
              v-for="u in assigneeCandidates"
              :key="u.id"
              :label="u.username"
              :value="u.id"
            />
          </el-select>
          <div v-if="assigneeCandidates.length === 0" class="form-hint">
            没有可选的处理人（当前系统里还没有已分派过工单的用户）
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="assignDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="assigning" @click="submitAssign">确认分派</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.detail-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-header {
  margin-bottom: 4px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.title {
  font-size: 16px;
  font-weight: 600;
}

.card-title {
  font-weight: 600;
}

.muted {
  color: #c0c4cc;
}

.description {
  margin-top: 18px;
}

.section-label {
  font-size: 13px;
  color: #909399;
  margin-bottom: 6px;
}

.description-text {
  white-space: pre-wrap;
  line-height: 1.7;
  color: #303133;
  background: #fafafa;
  padding: 12px;
  border-radius: 4px;
}

.actions {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.action-item {
  display: flex;
  align-items: center;
  gap: 12px;
}

.action-label {
  width: 80px;
  font-size: 13px;
  color: #606266;
}

.action-hint {
  font-size: 12px;
  color: #909399;
}

.comment-list {
  max-height: 380px;
  overflow-y: auto;
  margin-bottom: 16px;
}

.comment {
  padding: 10px 0;
  border-bottom: 1px solid #f0f2f5;
}

.comment:last-child {
  border-bottom: none;
}

.comment-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}

.comment-author {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}

.comment-time {
  font-size: 12px;
  color: #c0c4cc;
}

.comment-body {
  font-size: 14px;
  line-height: 1.6;
  color: #606266;
  white-space: pre-wrap;
}

.comment-submit {
  margin-top: 10px;
  width: 100%;
}

.timeline {
  padding-left: 4px;
  max-height: 520px;
  overflow-y: auto;
}

.event-text {
  font-size: 13px;
  color: #606266;
  line-height: 1.6;
}

.event-actor {
  font-weight: 600;
  color: #303133;
  margin-right: 4px;
}

.form-hint {
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}
</style>
