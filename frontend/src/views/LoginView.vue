<script setup lang="ts">
/**
 * 登录页。
 *
 * 一个 Vue 单文件组件（SFC）由三块组成：
 *   <script setup>  逻辑：变量、函数
 *   <template>      结构：页面上显示什么
 *   <style>         样式
 *
 * `setup` 的意思：这里定义的变量和函数，模板里可以直接用，
 * 不需要再 return 出去（这是 Vue 3 的语法糖）。
 */

import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { useUserStore } from '@/stores/user'
import type { LoginPayload } from '@/types/auth'

const router = useRouter()
const userStore = useUserStore()

/** 表单数据。reactive 让对象里的字段变化能被模板感知 */
const form = reactive<LoginPayload>({
  username: '',
  password: '',
})

/** 表单实例，用来调用 validate() */
const formRef = ref<FormInstance>()
/** 提交中标志，用来禁用按钮防止重复点击 */
const loading = ref(false)

/** 表单校验规则。冒号后面的数组里可以写多条规则 */
const rules: FormRules<LoginPayload> = {
  username: [{ required: true, message: '请输入用户名或邮箱', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function handleSubmit(): Promise<void> {
  // validate() 会按上面的 rules 检查，不通过就直接返回 false
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    await userStore.login(form)
    ElMessage.success(`欢迎回来，${userStore.displayName}`)
    // 登录成功跳转到工作台
    await router.push({ name: 'dashboard' })
  } catch {
    // 错误提示已经在 axios 响应拦截器里统一弹过了，
    // 这里只需要把 loading 关掉
  } finally {
    // finally 保证无论成功失败都会执行，不会出现"按钮一直转圈"
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <el-card class="auth-card">
      <div class="brand">
        <h1>OpsPilot</h1>
        <p>企业智能工单与运营协同平台</p>
      </div>

      <!--
        @submit.prevent 有两层意思：
          @submit  监听表单提交事件
          .prevent 阻止浏览器默认行为（默认会刷新整页）
        不写 .prevent 的话，点登录会整页刷新，Vue 的状态全丢。
      -->
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="handleSubmit"
      >
        <el-form-item label="用户名或邮箱" prop="username">
          <el-input
            v-model="form.username"
            placeholder="请输入用户名或邮箱"
            size="large"
            clearable
          />
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <!--
            show-password 加一个小眼睛图标，可以切换明文/密文
            @keyup.enter 回车直接提交，省得去点按钮
          -->
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            size="large"
            show-password
            @keyup.enter="handleSubmit"
          />
        </el-form-item>

        <el-button
          type="primary"
          size="large"
          class="submit-btn"
          :loading="loading"
          native-type="submit"
        >
          {{ loading ? '登录中...' : '登 录' }}
        </el-button>
      </el-form>

      <div class="footer">
        还没有账号？
        <router-link to="/register">立即注册</router-link>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
/* scoped 表示这些样式只作用于当前组件，不会污染其他页面 */
.auth-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%);
}

.auth-card {
  width: 400px;
  padding: 8px 12px;
}

.brand {
  text-align: center;
  margin-bottom: 28px;
}

.brand h1 {
  margin: 0 0 6px;
  font-size: 26px;
  color: #1e3a5f;
  letter-spacing: 1px;
}

.brand p {
  margin: 0;
  font-size: 13px;
  color: #909399;
}

.submit-btn {
  width: 100%;
  margin-top: 4px;
}

.footer {
  margin-top: 18px;
  text-align: center;
  font-size: 13px;
  color: #909399;
}
</style>
