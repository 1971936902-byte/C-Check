import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { authApi, TOKEN_KEY } from '../api/client'
import type { User } from '../types'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem(TOKEN_KEY) || '')
  const user = ref<User | null>(null)
  const isAuthenticated = computed(() => Boolean(token.value))
  const isAdmin = computed(() => user.value?.role === 'admin')
  async function login(username: string, password: string) {
    const { data } = await authApi.login(username, password)
    localStorage.setItem(TOKEN_KEY, data.access_token); token.value = data.access_token
    await fetchMe()
  }
  async function fetchMe() { const { data } = await authApi.me(); user.value = data; return data }
  function logout() { localStorage.removeItem(TOKEN_KEY); token.value = ''; user.value = null }
  return { token, user, isAuthenticated, isAdmin, login, fetchMe, logout }
})
