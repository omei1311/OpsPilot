<script setup lang="ts">
/**
 * 数据看板。
 *
 * 布局：概览卡片 → 折线图 + 饼图 → SLA 风险清单 + 处理人工作量
 */

import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'

import * as dashboardApi from '@/api/dashboard'
import type { DistributionResponse, TrendPoint, WorkloadItem } from '@/api/dashboard'
import type { TicketBrief } from '@/types/ticket'
import TicketTags from '@/components/ticket/TicketTags.vue'

const router = useRouter()

const loading = ref(false)
const overview = ref<dashboardApi.DashboardOverview | null>(null)
const distribution = ref<DistributionResponse | null>(null)
const slaRisk = ref<dashboardApi.SlaRiskResponse | null>(null)
const workload = ref<WorkloadItem[]>([])
const trendDays = ref(7)

// ── 图表实例 ──────────────────────────────────────────────
//
// ⚠️ ECharts 不是 Vue 组件，它需要一个真实的 DOM 容器。
// 用 ref 拿到 <div> 元素，然后 echarts.init(el) 在上面初始化。
// 图表的内容变化不走 Vue 的响应式，要手动调 setOption()。
const trendRef = ref<HTMLDivElement>()
const priorityRef = ref<HTMLDivElement>()
const categoryRef = ref<HTMLDivElement>()
const workloadRef = ref<HTMLDivElement>()

let trendChart: echarts.ECharts | null = null
let priorityChart: echarts.ECharts | null = null
let categoryChart: echarts.ECharts | null = null
let workloadChart: echarts.ECharts | null = null

// 统一配色。和 Element Plus 的语义色保持一致
const COLORS = ['#409eff', '#67c23a', '#e6a23c', '#f56c6c', '#909399', '#9b59b6']

// ── 加载数据 ──────────────────────────────────────────────
async function fetchAll(): Promise<void> {
  loading.value = true
  try {
    // Promise.all 让四个请求并发发出，而不是一个等一个。
    // 总耗时 ≈ 最慢的那个，而不是四个之和。
    const [ov, dist, risk, wl] = await Promise.all([
      dashboardApi.getOverview(),
      dashboardApi.getDistribution(),
      dashboardApi.getSlaRisk(8),
      dashboardApi.getWorkload(8),
    ])
    overview.value = ov
    distribution.value = dist
    slaRisk.value = risk
    workload.value = wl
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

async function fetchTrend(): Promise<void> {
  try {
    const res = await dashboardApi.getTrend(trendDays.value)
    renderTrend(res.points)
  } catch {
    // 拦截器已提示
  }
}

// ── 渲染图表 ──────────────────────────────────────────────

function renderTrend(points: TrendPoint[]): void {
  if (!trendRef.value) return
  trendChart ??= echarts.init(trendRef.value)

  trendChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['新增', '解决'], right: 0 },
    grid: { left: 40, right: 20, top: 40, bottom: 30 },
    xAxis: {
      type: 'category',
      // 只显示 月-日，年份占地方且每次都一样
      data: points.map((p) => p.date.slice(5)),
      boundaryGap: false,
    },
    yAxis: { type: 'value', minInterval: 1 },
    series: [
      {
        name: '新增',
        type: 'line',
        smooth: true,
        data: points.map((p) => p.created),
        itemStyle: { color: COLORS[0] },
        areaStyle: { opacity: 0.12 },
      },
      {
        name: '解决',
        type: 'line',
        smooth: true,
        data: points.map((p) => p.resolved),
        itemStyle: { color: COLORS[1] },
        areaStyle: { opacity: 0.12 },
      },
    ],
  })
}

function renderPie(
  el: HTMLDivElement | undefined,
  chart: echarts.ECharts | null,
  data: { name: string; value: number }[],
): echarts.ECharts | null {
  if (!el) return chart
  const instance = chart ?? echarts.init(el)

  instance.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, type: 'scroll' },
    color: COLORS,
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],
        center: ['50%', '45%'],
        avoidLabelOverlap: true,
        label: { formatter: '{b}\n{c}' },
        data,
      },
    ],
  })
  return instance
}

function renderWorkload(items: WorkloadItem[]): void {
  if (!workloadRef.value) return
  workloadChart ??= echarts.init(workloadRef.value)

  workloadChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { data: ['处理中', '已解决'], right: 0 },
    grid: { left: 40, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: items.map((i) => i.username) },
    yAxis: { type: 'value', minInterval: 1 },
    series: [
      {
        name: '处理中',
        type: 'bar',
        stack: 'total',
        data: items.map((i) => i.processing),
        itemStyle: { color: COLORS[0] },
        barMaxWidth: 36,
      },
      {
        name: '已解决',
        type: 'bar',
        stack: 'total',
        data: items.map((i) => i.resolved),
        itemStyle: { color: COLORS[1] },
        barMaxWidth: 36,
      },
    ],
  })
}

function renderAllCharts(): void {
  if (distribution.value) {
    priorityChart = renderPie(priorityRef.value, priorityChart, distribution.value.by_priority)
    categoryChart = renderPie(categoryRef.value, categoryChart, distribution.value.by_category)
  }
  renderWorkload(workload.value)
}

/**
 * 窗口大小变化时要让图表跟着重算尺寸，
 * 否则拉窄浏览器后图表还是旧尺寸，会溢出或留白。
 */
function handleResize(): void {
  trendChart?.resize()
  priorityChart?.resize()
  categoryChart?.resize()
  workloadChart?.resize()
}

// 数据到了就重绘（因为图表渲染依赖异步拿到的数据）
watch([distribution, workload], renderAllCharts)
watch(trendDays, fetchTrend)

function goTicket(row: TicketBrief): void {
  router.push({ name: 'ticket-detail', params: { id: row.id } })
}

function formatTime(value: string): string {
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

onMounted(async () => {
  window.addEventListener('resize', handleResize)
  await fetchAll()
  await fetchTrend()
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  // ★ 必须销毁图表实例，否则组件卸载后 ECharts 还持有 DOM 引用，
  // 反复进出页面会造成内存泄漏。
  trendChart?.dispose()
  priorityChart?.dispose()
  categoryChart?.dispose()
  workloadChart?.dispose()
})
</script>

<template>
  <div v-loading="loading" class="dashboard">
    <!-- ── 概览卡片 ─────────────────────────────────── -->
    <el-row :gutter="16">
      <el-col v-for="card in [
        { label: '工单总数', value: overview?.total, color: '#409eff' },
        { label: '今日新增', value: overview?.today_created, color: '#67c23a' },
        { label: '待处理', value: overview?.pending, color: '#e6a23c' },
        { label: '处理中', value: overview?.processing, color: '#409eff' },
        { label: '已解决', value: overview?.resolved, color: '#67c23a' },
        { label: 'SLA 超时', value: overview?.overdue, color: '#f56c6c' },
      ]" :key="card.label" :xs="12" :sm="8" :md="4">
        <el-card shadow="never" class="stat-card">
          <div class="stat-value" :style="{ color: card.color }">{{ card.value ?? '-' }}</div>
          <div class="stat-label">{{ card.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- ── 趋势 + 分布 ──────────────────────────────── -->
    <el-row :gutter="16" class="row">
      <el-col :xs="24" :lg="14">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span class="card-title">工单趋势</span>
              <el-radio-group v-model="trendDays" size="small">
                <el-radio-button :value="7">7 天</el-radio-button>
                <el-radio-button :value="14">14 天</el-radio-button>
                <el-radio-button :value="30">30 天</el-radio-button>
              </el-radio-group>
            </div>
          </template>
          <div ref="trendRef" class="chart chart-lg"></div>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="10">
        <el-card shadow="never">
          <template #header><span class="card-title">优先级分布</span></template>
          <div ref="priorityRef" class="chart chart-lg"></div>
        </el-card>
      </el-col>
    </el-row>

    <!-- ── SLA 风险 + 工作量 ────────────────────────── -->
    <el-row :gutter="16" class="row">
      <el-col :xs="24" :lg="14">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span class="card-title">SLA 风险工单</span>
              <span class="header-hint">
                已超时 <b class="danger">{{ slaRisk?.total_overdue ?? 0 }}</b> 条 ·
                24 小时内到期 <b class="warning">{{ slaRisk?.total_at_risk ?? 0 }}</b> 条
              </span>
            </div>
          </template>

          <el-table :data="slaRisk?.items ?? []" size="small" @row-click="goTicket">
            <el-table-column prop="ticket_no" label="工单号" width="150" />
            <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
            <el-table-column label="状态" width="180">
              <template #default="{ row }">
                <TicketTags :status="row.status" :priority="row.priority" :sla-status="row.sla_status" />
              </template>
            </el-table-column>
            <el-table-column label="截止" width="110">
              <template #default="{ row }">{{ formatTime(row.sla_deadline) }}</template>
            </el-table-column>
            <template #empty>
              <el-empty description="没有 SLA 风险工单" :image-size="60" />
            </template>
          </el-table>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="10">
        <el-card shadow="never" class="row">
          <template #header><span class="card-title">处理人工作量</span></template>
          <div ref="workloadRef" class="chart chart-md"></div>
        </el-card>

        <el-card shadow="never" class="row">
          <template #header><span class="card-title">分类分布</span></template>
          <div ref="categoryRef" class="chart chart-md"></div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.dashboard {
  display: flex;
  flex-direction: column;
}

.stat-card {
  text-align: center;
  margin-bottom: 16px;
}

.stat-value {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.3;
}

.stat-label {
  font-size: 13px;
  color: #909399;
  margin-top: 4px;
}

.row {
  margin-bottom: 16px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.card-title {
  font-weight: 600;
}

.header-hint {
  font-size: 13px;
  color: #909399;
}

.danger {
  color: #f56c6c;
}

.warning {
  color: #e6a23c;
}

.chart {
  width: 100%;
}

.chart-lg {
  height: 300px;
}

.chart-md {
  height: 220px;
}

:deep(.el-table__row) {
  cursor: pointer;
}
</style>
