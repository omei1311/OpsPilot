<script setup lang="ts">
/**
 * 工单列表页。
 *
 * 结构：筛选栏 → 表格 → 分页
 */

import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import * as ticketApi from '@/api/ticket'
import { useUserStore } from '@/stores/user'
import {
  CATEGORY_LABELS,
  PRIORITY_LABELS,
  STATUS_LABELS,
  type TicketBrief,
  type TicketCategory,
  type TicketCreatePayload,
  type TicketListParams,
  type TicketPriority,
  type TicketStatus,
} from '@/types/ticket'
import TicketTags from '@/components/ticket/TicketTags.vue'

const router = useRouter()
const userStore = useUserStore()

// ── 状态 ──────────────────────────────────────────────────
const loading = ref(false)
const tickets = ref<TicketBrief[]>([])
const total = ref(0)

/** 筛选条件。用 reactive 让对象的每个字段都能被 el-form 双向绑定 */
const filters = reactive({
  keyword: '',
  status: [] as TicketStatus[],
  priority: [] as TicketPriority[],
  category: [] as TicketCategory[],
  /** 快捷筛选：超过 N 小时未处理 */
  unhandled_hours: undefined as number | undefined,
})

const pagination = reactive({ page: 1, page_size: 20 })

// ── 下拉选项 ──────────────────────────────────────────────
const statusOptions = Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }))
const priorityOptions = Object.entries(PRIORITY_LABELS).map(([value, label]) => ({ value, label }))
const categoryOptions = Object.entries(CATEGORY_LABELS).map(([value, label]) => ({ value, label }))

/** 快捷筛选按钮。「超过 24 小时未处理」正是场景 4 的那个条件 */
const quickFilters = [
  { label: '全部', value: undefined },
  { label: '超过 24 小时未处理', value: 24 },
  { label: '超过 48 小时未处理', value: 48 },
]

const activeQuick = computed(() => filters.unhandled_hours)

// ── 数据加载 ──────────────────────────────────────────────
async function fetchList(): Promise<void> {
  loading.value = true
  try {
    const params: TicketListParams = {
      page: pagination.page,
      page_size: pagination.page_size,
    }
    if (filters.keyword) params.keyword = filters.keyword
    if (filters.status.length) params.status = filters.status
    if (filters.priority.length) params.priority = filters.priority
    if (filters.category.length) params.category = filters.category
    if (filters.unhandled_hours) params.unhandled_hours = filters.unhandled_hours

    const result = await ticketApi.listTickets(params)
    tickets.value = result.items
    total.value = result.total
  } catch {
    // 错误提示已由响应拦截器统一处理
  } finally {
    loading.value = false
  }
}

/** 点搜索 / 改筛选条件时，页码必须回到第 1 页 */
function handleSearch(): void {
  pagination.page = 1
  fetchList()
}

function handleReset(): void {
  filters.keyword = ''
  filters.status = []
  filters.priority = []
  filters.category = []
  filters.unhandled_hours = undefined
  pagination.page = 1
  fetchList()
}

function applyQuickFilter(value?: number): void {
  filters.unhandled_hours = value
  handleSearch()
}

function handlePageChange(page: number): void {
  pagination.page = page
  fetchList()
}

function goDetail(row: TicketBrief): void {
  router.push({ name: 'ticket-detail', params: { id: row.id } })
}

// ── 新建工单 ──────────────────────────────────────────────
const createVisible = ref(false)
const creating = ref(false)
const createForm = reactive<TicketCreatePayload>({
  title: '',
  description: '',
  category: 'other',
  priority: 'medium',
})

function openCreate(): void {
  createForm.title = ''
  createForm.description = ''
  createForm.category = 'other'
  createForm.priority = 'medium'
  createVisible.value = true
}

async function submitCreate(): Promise<void> {
  if (!createForm.title || createForm.title.trim().length < 2) {
    ElMessage.warning('标题至少 2 个字')
    return
  }
  creating.value = true
  try {
    const created = await ticketApi.createTicket(createForm)
    ElMessage.success(`工单 ${created.ticket_no} 创建成功`)
    createVisible.value = false
    pagination.page = 1
    await fetchList()
  } catch {
    // 拦截器已提示
  } finally {
    creating.value = false
  }
}

// ── 删除 ──────────────────────────────────────────────────
async function handleDelete(row: TicketBrief): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定删除工单 ${row.ticket_no} 吗？此操作不可恢复。`,
      '删除确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await ticketApi.deleteTicket(row.id)
    ElMessage.success('已删除')
    fetchList()
  } catch {
    // 拦截器已提示
  }
}

/** 时间格式化：后端给的是 UTC（带 Z），浏览器会自动转本地时区 */
function formatTime(value: string): string {
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

onMounted(fetchList)
</script>

<template>
  <div class="ticket-page">
    <!-- ── 筛选栏 ─────────────────────────────────────── -->
    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent="handleSearch">
        <el-form-item label="关键词">
          <el-input
            v-model="filters.keyword"
            placeholder="标题或描述"
            clearable
            style="width: 200px"
            @keyup.enter="handleSearch"
          />
        </el-form-item>

        <el-form-item label="状态">
          <el-select
            v-model="filters.status"
            multiple
            collapse-tags
            placeholder="全部"
            clearable
            style="width: 170px"
          >
            <el-option
              v-for="op in statusOptions"
              :key="op.value"
              :label="op.label"
              :value="op.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="优先级">
          <el-select
            v-model="filters.priority"
            multiple
            collapse-tags
            placeholder="全部"
            clearable
            style="width: 150px"
          >
            <el-option
              v-for="op in priorityOptions"
              :key="op.value"
              :label="op.label"
              :value="op.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="分类">
          <el-select
            v-model="filters.category"
            multiple
            collapse-tags
            placeholder="全部"
            clearable
            style="width: 160px"
          >
            <el-option
              v-for="op in categoryOptions"
              :key="op.value"
              :label="op.label"
              :value="op.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" @click="handleSearch">查询</el-button>
          <el-button @click="handleReset">重置</el-button>
        </el-form-item>
      </el-form>

      <!-- 快捷筛选 -->
      <div class="quick-filters">
        <span class="quick-label">快捷筛选：</span>
        <el-button
          v-for="q in quickFilters"
          :key="q.label"
          size="small"
          :type="activeQuick === q.value ? 'primary' : 'default'"
          @click="applyQuickFilter(q.value)"
        >
          {{ q.label }}
        </el-button>
      </div>
    </el-card>

    <!-- ── 表格 ───────────────────────────────────────── -->
    <el-card shadow="never">
      <template #header>
        <div class="table-header">
          <span>工单列表<span class="total-hint">共 {{ total }} 条</span></span>
          <el-button type="primary" @click="openCreate">
            <el-icon><Plus /></el-icon>
            新建工单
          </el-button>
        </div>
      </template>

      <el-table
        v-loading="loading"
        :data="tickets"
        stripe
        style="width: 100%"
        @row-click="goDetail"
      >
        <el-table-column prop="ticket_no" label="工单号" width="160" />

        <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip />

        <el-table-column label="分类" width="90">
          <template #default="{ row }">
            {{ CATEGORY_LABELS[row.category as TicketCategory] }}
          </template>
        </el-table-column>

        <el-table-column label="状态 / 优先级 / SLA" width="260">
          <template #default="{ row }">
            <TicketTags :status="row.status" :priority="row.priority" :sla-status="row.sla_status" />
          </template>
        </el-table-column>

        <el-table-column label="负责人" width="110">
          <template #default="{ row }">
            <span v-if="row.assignee_name">{{ row.assignee_name }}</span>
            <span v-else class="muted">未分派</span>
          </template>
        </el-table-column>

        <el-table-column label="创建时间" width="150">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>

        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <!--
              .stop 阻止事件冒泡。
              表格行本身绑了 @row-click 跳详情，
              不阻止的话点删除按钮会先跳走再弹确认框。
            -->
            <el-button link type="primary" size="small" @click.stop="goDetail(row)">
              详情
            </el-button>
            <el-button
              v-if="userStore.isAdmin"
              link
              type="danger"
              size="small"
              @click.stop="handleDelete(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="没有符合条件的工单" />
        </template>
      </el-table>

      <el-pagination
        v-if="total > 0"
        class="pagination"
        layout="total, prev, pager, next, sizes"
        :total="total"
        :current-page="pagination.page"
        :page-size="pagination.page_size"
        :page-sizes="[10, 20, 50, 100]"
        @current-change="handlePageChange"
        @size-change="
          (size: number) => {
            pagination.page_size = size
            handlePageChange(1)
          }
        "
      />
    </el-card>

    <!-- ── 新建工单弹窗 ───────────────────────────────── -->
    <el-dialog v-model="createVisible" title="新建工单" width="540px">
      <el-form :model="createForm" label-width="80px">
        <el-form-item label="标题" required>
          <el-input v-model="createForm.title" placeholder="简要描述问题" maxlength="200" show-word-limit />
        </el-form-item>

        <el-form-item label="详细描述">
          <el-input
            v-model="createForm.description"
            type="textarea"
            :rows="4"
            placeholder="补充详细信息，便于定位问题"
            maxlength="5000"
          />
        </el-form-item>

        <el-form-item label="分类">
          <el-select v-model="createForm.category" style="width: 100%">
            <el-option
              v-for="op in categoryOptions"
              :key="op.value"
              :label="op.label"
              :value="op.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="优先级">
          <el-select v-model="createForm.priority" style="width: 100%">
            <el-option
              v-for="op in priorityOptions"
              :key="op.value"
              :label="op.label"
              :value="op.value"
            />
          </el-select>
          <div class="form-hint">优先级决定 SLA 截止时间，创建后可在详情页调整</div>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ticket-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.filter-card :deep(.el-form-item) {
  margin-bottom: 12px;
}

.quick-filters {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 8px;
  border-top: 1px dashed #e4e7ed;
}

.quick-label {
  font-size: 13px;
  color: #909399;
}

.table-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
}

.total-hint {
  margin-left: 10px;
  font-size: 13px;
  font-weight: 400;
  color: #909399;
}

.muted {
  color: #c0c4cc;
}

.pagination {
  margin-top: 16px;
  justify-content: flex-end;
}

.form-hint {
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}

:deep(.el-table__row) {
  cursor: pointer;
}
</style>
