<script setup lang="ts">
/**
 * Agent 执行过程时间线。
 *
 * 这是"可观测性"在前端的体现 —— 用户能看到 AI 到底做了什么，
 * 而不是只看到一个最终答案。
 *
 * 设计要点：
 *   · 工具调用做成可折叠的卡片，默认收起，避免刷屏
 *   · 思考文本用浅灰小字，视觉上退到背景
 *   · 每一步带耗时，能看出哪一步慢
 */

import { computed, ref } from 'vue'

import { INTENT_LABELS, TOOL_LABELS, type AgentStep } from '@/types/agent'

const props = defineProps<{
  steps: AgentStep[]
  /** 是否正在流式（决定要不要显示"进行中"的转圈） */
  streaming?: boolean
}>()

/** 哪些工具卡片被展开了。用 Set 存 key，和 v-for 的 key 对应 */
const expanded = ref<Set<string>>(new Set())

function toggleExpand(key: string): void {
  // ⚠️ Set 是引用类型，直接 mutate 不会触发 Vue 的响应式更新。
  // 必须重新赋值一个新的 Set，或者用 reactive。
  // 这是 Vue 响应式的一个常见坑。
  const next = new Set(expanded.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expanded.value = next
}

/** 是否显示"进行中" */
const showProgress = computed(() => props.streaming === true)

function formatArgs(args?: Record<string, any>): string {
  if (!args || Object.keys(args).length === 0) return '无参数'
  return JSON.stringify(args, null, 2)
}
</script>

<template>
  <div class="timeline">
    <div v-for="step in steps" :key="step.key" class="step">
      <!-- ── 意图 ─────────────────────────────────── -->
      <div v-if="step.type === 'intent'" class="step-intent">
        <el-icon class="icon"><Aim /></el-icon>
        <span>识别为「{{ INTENT_LABELS[step.intent ?? ''] ?? step.intent }}」</span>
        <span v-if="step.confidence" class="dim">
          {{ Math.round((step.confidence ?? 0) * 100) }}%
        </span>
      </div>

      <!-- ── 思考 ─────────────────────────────────── -->
      <div v-else-if="step.type === 'thought'" class="step-thought">
        {{ step.text }}
      </div>

      <!-- ── 工具调用 ─────────────────────────────── -->
      <div v-else-if="step.type === 'tool_call'" class="step-tool">
        <div class="tool-head" @click="toggleExpand(step.key)">
          <el-icon class="icon" :class="{ spin: step.ok === undefined }">
            <Loading v-if="step.ok === undefined" />
            <CircleCheck v-else-if="step.ok" style="color: #67c23a" />
            <CircleClose v-else style="color: #f56c6c" />
          </el-icon>

          <span class="tool-name">{{ TOOL_LABELS[step.tool ?? ''] ?? step.tool }}</span>

          <span v-if="step.summary" class="tool-summary">{{ step.summary }}</span>
          <span v-else-if="step.ok === undefined" class="tool-summary dim">执行中...</span>

          <span v-if="step.durationMs !== undefined" class="dim tool-time">
            {{ step.durationMs }}ms
          </span>

          <el-icon class="expand-icon">
            <ArrowRight v-if="!expanded.has(step.key)" />
            <ArrowDown v-else />
          </el-icon>
        </div>

        <div v-if="expanded.has(step.key)" class="tool-detail">
          <div class="detail-label">调用参数</div>
          <pre class="detail-code">{{ formatArgs(step.args) }}</pre>
        </div>
      </div>

      <!-- ── 计划 ─────────────────────────────────── -->
      <div v-else-if="step.type === 'plan'" class="step-plan">
        <el-icon class="icon"><WarningFilled /></el-icon>
        <span>已生成执行计划：{{ step.text }}</span>
      </div>

      <!-- ── 错误 ─────────────────────────────────── -->
      <div v-else-if="step.type === 'error'" class="step-error">
        <el-icon class="icon"><CircleCloseFilled /></el-icon>
        <span>{{ step.text }}</span>
      </div>
    </div>

    <div v-if="showProgress && steps.length === 0" class="step-thought dim">
      正在思考...
    </div>
  </div>
</template>

<style scoped>
.timeline {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 8px;
}

.step {
  font-size: 12px;
  line-height: 1.6;
}

/* 意图 */
.step-intent {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #409eff;
  background: #ecf5ff;
  padding: 4px 8px;
  border-radius: 4px;
}

/* 思考 */
.step-thought {
  color: #909399;
  padding: 2px 8px;
  white-space: pre-wrap;
}

/* 工具 */
.step-tool {
  background: #f8f9fb;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  overflow: hidden;
}

.tool-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  cursor: pointer;
  user-select: none;
}

.tool-head:hover {
  background: #f0f2f5;
}

.tool-name {
  font-weight: 600;
  color: #303133;
}

.tool-summary {
  color: #67c23a;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-time {
  font-size: 11px;
}

.expand-icon {
  color: #c0c4cc;
  font-size: 12px;
}

.tool-detail {
  border-top: 1px solid #ebeef5;
  padding: 8px;
  background: #fff;
}

.detail-label {
  font-size: 11px;
  color: #909399;
  margin-bottom: 4px;
}

.detail-code {
  margin: 0;
  font-size: 11px;
  font-family: 'Consolas', 'Monaco', monospace;
  color: #606266;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow-y: auto;
}

/* 计划 */
.step-plan {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #e6a23c;
  background: #fdf6ec;
  padding: 4px 8px;
  border-radius: 4px;
}

/* 错误 */
.step-error {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #f56c6c;
  background: #fef0f0;
  padding: 4px 8px;
  border-radius: 4px;
}

.icon {
  font-size: 13px;
}

.dim {
  color: #c0c4cc;
}

/* 转圈动画 */
.spin {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
