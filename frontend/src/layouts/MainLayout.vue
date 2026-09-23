<script setup lang="ts">
/**
 * 主布局：左侧菜单 + 顶部栏 + 内容区。
 *
 * 用布局组件的意义：登录后所有页面共享同一套导航，
 * 不需要每个页面各自写一遍侧边栏。
 * 子页面通过 <router-view /> 渲染到这个布局的中间区域。
 */

import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'

import AgentPanel from '@/components/agent/AgentPanel.vue'
import { useAgentStore } from '@/stores/agent'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const agentStore = useAgentStore()

// 进主布局时加载一次会话列表，这样打开面板就能看到历史会话
onMounted(() => {
  agentStore.loadConversations()
})

/** 当前高亮的菜单项，跟着路由自动变化 */
const activeMenu = computed(() => route.path)

const ROLE_LABELS: Record<string, string> = {
  admin: '管理员',
  operator: '操作员',
  user: '普通用户',
}

async function handleLogout(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定要退出登录吗？', '提示', {
      confirmButtonText: '退出',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  userStore.logout()
  await router.push({ name: 'login' })
}
</script>

<template>
  <el-container class="layout">
    <!-- ── 左侧菜单 ─────────────────────────────────── -->
    <el-aside width="210px" class="aside">
      <div class="logo">
        <span class="logo-title">OpsPilot</span>
        <span class="logo-sub">智能工单平台</span>
      </div>

      <!--
        router 属性：开启后 el-menu-item 的 index 会被当成路由路径，
        点击直接跳转，不需要自己写 @click
      -->
      <el-menu :default-active="activeMenu" router class="menu">
        <el-menu-item index="/dashboard">
          <el-icon><DataLine /></el-icon>
          <span>数据看板</span>
        </el-menu-item>
        <el-menu-item index="/tickets">
          <el-icon><Tickets /></el-icon>
          <span>工单中心</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <!-- ── 顶部栏 ─────────────────────────────────── -->
      <el-header class="header">
        <div class="header-title">
          {{ route.meta.title ?? '' }}
        </div>

        <el-dropdown>
          <span class="user-info">
            <el-avatar :size="30" class="avatar">
              {{ userStore.displayName.charAt(0).toUpperCase() }}
            </el-avatar>
            <span class="username">{{ userStore.displayName }}</span>
            <el-tag size="small" type="info">
              {{ ROLE_LABELS[userStore.profile?.role ?? ''] ?? userStore.profile?.role }}
            </el-tag>
          </span>

          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="handleLogout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>

      <!-- ── 内容区 ─────────────────────────────────── -->
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>

    <!--
      ★ AI 助手面板。
      放在 el-container 外面，因为它用的是 position: fixed 悬浮定位，
      不属于正常的文档流布局。放在里面会被 el-main 的 overflow 裁掉。
    -->
    <AgentPanel />
  </el-container>
</template>

<style scoped>
.layout {
  height: 100vh;
}

.aside {
  background: #1e3a5f;
  display: flex;
  flex-direction: column;
}

.logo {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.logo-title {
  color: #fff;
  font-size: 19px;
  font-weight: 600;
  letter-spacing: 1px;
}

.logo-sub {
  color: rgba(255, 255, 255, 0.5);
  font-size: 12px;
}

.menu {
  border-right: none;
  background: transparent;
  flex: 1;
}

/*
  el-menu 的默认样式是浅色背景，在深色侧边栏上要覆盖。
  :deep() 是 Vue 的深度选择器 —— scoped 样式默认只作用于当前组件，
  但要改 Element Plus 组件内部的元素就必须用 :deep() 穿透进去。
*/
.menu :deep(.el-menu-item) {
  color: rgba(255, 255, 255, 0.75);
}

.menu :deep(.el-menu-item:hover) {
  background: rgba(255, 255, 255, 0.08);
  color: #fff;
}

.menu :deep(.el-menu-item.is-active) {
  background: rgba(255, 255, 255, 0.14);
  color: #fff;
  border-right: 3px solid #409eff;
}

.header {
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  outline: none;
}

.avatar {
  background: #409eff;
  color: #fff;
  font-size: 14px;
}

.username {
  font-size: 14px;
  color: #303133;
}

.main {
  background: #f5f7fa;
  padding: 20px;
  overflow-y: auto;
}
</style>
