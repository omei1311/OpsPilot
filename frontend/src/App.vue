<script setup lang="ts">
/**
 * 根组件。
 *
 * 它的全部工作就是「把当前路由对应的页面显示出来」。
 * <router-view /> 是占位符，Vue Router 会把匹配到的组件塞进这里：
 *     访问 /login     → 这里变成 LoginView
 *     访问 /dashboard → 这里变成 DashboardView
 */
</script>

<template>
  <router-view />
</template>
