<script setup lang="ts">
/**
 * 工单标签组：状态 / 优先级 / SLA。
 *
 * 抽成组件的原因：这三个标签在列表页、详情页、风险清单里都要用，
 * 样式和文案必须一致。写三遍迟早会不一致。
 */

import { computed } from 'vue'

import {
  PRIORITY_LABELS,
  PRIORITY_TAG_TYPE,
  SLA_LABELS,
  SLA_TAG_TYPE,
  STATUS_LABELS,
  STATUS_TAG_TYPE,
  type SlaStatus,
  type TicketPriority,
  type TicketStatus,
} from '@/types/ticket'

const props = defineProps<{
  status?: TicketStatus
  priority?: TicketPriority
  slaStatus?: SlaStatus | null
  /** SLA 剩余分钟数，负数表示已超时 */
  slaRemaining?: number | null
}>()

/** 把剩余分钟数格式化成「2小时15分」/「已超时 3小时」 */
const slaText = computed(() => {
  if (props.slaRemaining == null) return null

  const minutes = props.slaRemaining
  const abs = Math.abs(minutes)
  const hours = Math.floor(abs / 60)
  const mins = abs % 60

  // 组合出时长部分
  let duration: string
  if (hours >= 24) {
    duration = `${Math.floor(hours / 24)}天${hours % 24}小时`
  } else if (hours > 0) {
    duration = mins > 0 ? `${hours}小时${mins}分` : `${hours}小时`
  } else {
    duration = `${mins}分钟`
  }

  return minutes < 0 ? `已超时 ${duration}` : `剩 ${duration}`
})
</script>

<template>
  <span class="tags">
    <el-tag v-if="status" :type="STATUS_TAG_TYPE[status] as any" size="small" disable-transitions>
      {{ STATUS_LABELS[status] }}
    </el-tag>

    <el-tag
      v-if="priority"
      :type="PRIORITY_TAG_TYPE[priority] as any"
      size="small"
      effect="plain"
      disable-transitions
    >
      {{ PRIORITY_LABELS[priority] }}
    </el-tag>

    <el-tooltip v-if="slaStatus" :content="slaText ?? ''" :disabled="!slaText" placement="top">
      <el-tag
        :type="SLA_TAG_TYPE[slaStatus] as any"
        size="small"
        effect="dark"
        disable-transitions
      >
        {{ slaText ?? SLA_LABELS[slaStatus] }}
      </el-tag>
    </el-tooltip>
  </span>
</template>

<style scoped>
.tags {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}
</style>
