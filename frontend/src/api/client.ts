import axios from 'axios'
import { ElMessage } from 'element-plus'

const client = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
})

client.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

client.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status
    const message =
      error.response?.data?.message || error.message || '请求失败'
    if (status === 401) {
      localStorage.removeItem('access_token')
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login'
      }
    } else if (!error.config?.silent) {
      ElMessage.error(message)
    }
    return Promise.reject(error)
  },
)

export default client

export interface ApiError {
  code: string
  message: string
  details?: unknown
}
