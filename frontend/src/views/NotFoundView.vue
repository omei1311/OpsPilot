<script setup lang="ts">
/** 404 页。路由表最后那条 :pathMatch(.*)* 会匹配到这里。 */
</script>

<template>
  <div class="not-found">
    <h1>404</h1>
    <p>你访问的页面不存在</p>
    <router-link to="/dashboard">
      <el-button type="primary">返回工作台</el-button>
    </router-link>
  </div>
</template>

<style scoped>
.not-found {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: #606266;
}

.not-found h1 {
  font-size: 72px;
  margin: 0;
  color: #dcdfe6;
}
</style>
