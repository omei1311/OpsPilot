<script setup lang="ts">
/** 注册页。结构和登录页几乎一样，多了一个"确认密码"和邮箱格式校验。 */

import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()

/** 表单比后端多一个 confirmPassword，提交前要把它剔掉 */
const form = reactive({
  username: '',
  email: '',
  password: '',
  confirmPassword: '',
})

const formRef = ref<FormInstance>()
const loading = ref(false)

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    // 这里的前后端双重校验不是重复劳动：
    // 前端校验是为了体验（立刻提示），
    // 后端校验是为了安全（前端可以被绕过）。
    { min: 3, max: 50, message: '用户名长度 3-50 个字符', trigger: 'blur' },
  ],
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 72, message: '密码长度 6-72 个字符', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      // 自定义校验函数：value 是当前字段的值
      validator: (_rule, value: string, callback) => {
        if (value !== form.password) {
          callback(new Error('两次输入的密码不一致'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

async function handleSubmit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    // 只把后端需要的三个字段发过去，confirmPassword 不传
    await userStore.register({
      username: form.username,
      email: form.email,
      password: form.password,
    })
    ElMessage.success('注册成功，请登录')
    await router.push({ name: 'login' })
  } catch {
    // 错误提示已在响应拦截器里统一处理
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <el-card class="auth-card">
      <div class="brand">
        <h1>注册账号</h1>
        <p>加入 OpsPilot</p>
      </div>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="handleSubmit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="3-50 个字符" size="large" />
        </el-form-item>

        <el-form-item label="邮箱" prop="email">
          <el-input v-model="form.email" placeholder="you@example.com" size="large" />
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="6-72 个字符"
            size="large"
            show-password
          />
        </el-form-item>

        <el-form-item label="确认密码" prop="confirmPassword">
          <el-input
            v-model="form.confirmPassword"
            type="password"
            placeholder="请再次输入密码"
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
          {{ loading ? '注册中...' : '注 册' }}
        </el-button>
      </el-form>

      <div class="footer">
        已有账号？
        <router-link to="/login">返回登录</router-link>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.auth-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%);
  padding: 24px 0;
}

.auth-card {
  width: 400px;
  padding: 8px 12px;
}

.brand {
  text-align: center;
  margin-bottom: 24px;
}

.brand h1 {
  margin: 0 0 6px;
  font-size: 24px;
  color: #1e3a5f;
}

.brand p {
  margin: 0;
  font-size: 13px;
  color: #909399;
}

.submit-btn {
  width: 100%;
}

.footer {
  margin-top: 18px;
  text-align: center;
  font-size: 13px;
  color: #909399;
}
</style>
