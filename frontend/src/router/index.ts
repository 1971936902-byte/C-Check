import { createRouter, createWebHistory } from 'vue-router'
import { pinia } from '../stores'
import { useAuthStore } from '../stores/auth'
import LoginView from '../views/LoginView.vue'

const AppLayout = () => import('../layouts/AppLayout.vue')
const WorkspaceView = () => import('../views/WorkspaceView.vue')
const ReportView = () => import('../views/ReportView.vue')
const HistoryView = () => import('../views/HistoryView.vue')
const ProfileView = () => import('../views/ProfileView.vue')
const AdminView = () => import('../views/AdminView.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: LoginView, meta: { public: true } },
    { path: '/', component: AppLayout, children: [
      { path: '', redirect: '/workspace' },
      { path: 'workspace', component: WorkspaceView },
      { path: 'reports/:id', component: ReportView },
      { path: 'history', component: HistoryView },
      { path: 'profile', component: ProfileView },
      { path: 'admin', component: AdminView, meta: { admin: true } },
    ] },
  ],
})
router.beforeEach(async (to) => {
  const auth = useAuthStore(pinia)
  if (to.meta.public) return auth.isAuthenticated ? '/workspace' : true
  if (!auth.isAuthenticated) return '/login'
  if (!auth.user) { try { await auth.fetchMe() } catch { auth.logout(); return '/login' } }
  if (to.meta.admin && !auth.isAdmin) return '/workspace'
  return true
})
export default router
