'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { resetSession } from '@/lib/analysis-session';

const TOKEN_KEY = 'resume-screening-token';
const USER_KEY = 'resume-screening-user';

export interface AuthUser {
  user_id: string;
  username: string;
  email?: string;
  is_admin?: boolean;
  is_super_admin?: boolean;
}

export function getStoredToken(): string {
  if (typeof window === 'undefined') return '';
  return window.localStorage.getItem(TOKEN_KEY) || '';
}

export function getStoredUser(): AuthUser | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as AuthUser) : null;
  } catch {
    return null;
  }
}

export function clearStoredAuth(): void {
  if (typeof window === 'undefined') return;
  // localStorage：持久登录态（关闭浏览器后仍保持）
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
  // 清理 sessionStorage 残留（会话隔离改版迁移兼容）
  window.sessionStorage.removeItem(TOKEN_KEY);
  window.sessionStorage.removeItem(USER_KEY);
}

/** 服务端 /api/v1/auth/me 兜底刷新 is_admin/is_super_admin/email（登录响应不含这些字段）。 */
export async function refreshProfile(): Promise<AuthUser | null> {
  const token = getStoredToken();
  if (!token) return null;
  try {
    const base = (process.env.NEXT_PUBLIC_API_URL || '').trim();
    const response = await fetch(`${base}/api/v1/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) return null;
    const payload = (await response.json()) as {
      data?: { user_id?: string; username?: string; email?: string; is_admin?: boolean; is_super_admin?: boolean };
    };
    const data = payload.data;
    if (!data?.user_id) return null;
    const stored = getStoredUser();
    return {
      user_id: data.user_id,
      username: data.username || stored?.username || '',
      email: data.email || stored?.email || '',
      is_admin: Boolean(data.is_admin),
      is_super_admin: Boolean(data.is_super_admin),
    };
  } catch {
    return getStoredUser();
  }
}

interface AuthContextValue {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isHydrated: boolean;
  isAdmin: boolean;
  isSuperAdmin: boolean;
  login: (token: string, user: AuthUser) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isHydrated, setIsHydrated] = useState(false);

  useEffect(() => {
    // 首次挂载：从 localStorage 恢复登录态（持久），并调用 /auth/me 刷新管理员标记
    const token = getStoredToken();
    const storedUser = getStoredUser();
    setUser(token && storedUser ? storedUser : null);
    setIsHydrated(true);
    if (!token) return;
    (async () => {
      const fresh = await refreshProfile();
      if (fresh) {
        window.localStorage.setItem(USER_KEY, JSON.stringify(fresh));
        setUser(fresh);
      }
    })();
  }, []);

  const isOnLoginPage = pathname === '/login';
  // 公开页面（未登录也可访问）：登录页 + 忘记密码重置页
  const isPublicPage = isOnLoginPage || pathname === '/reset-password';

  useEffect(() => {
    if (!isHydrated) return;
    const token = getStoredToken();
    if (!token && !isPublicPage) {
      // 未登录访问受保护页面 → 跳登录
      router.replace('/login');
    }
  }, [isHydrated, isPublicPage, router]);

  const login = useCallback((token: string, authUser: AuthUser) => {
    // 写入 localStorage：持久登录态（关闭浏览器后仍保持）
    window.localStorage.setItem(TOKEN_KEY, token);
    window.localStorage.setItem(USER_KEY, JSON.stringify(authUser));
    setUser(authUser);
    // 异步刷新管理员标记（登录响应不含 email/is_admin）
    (async () => {
      const fresh = await refreshProfile();
      if (fresh) {
        window.localStorage.setItem(USER_KEY, JSON.stringify(fresh));
        setUser(fresh);
      }
    })();
  }, []);

  const logout = useCallback(() => {
    clearStoredAuth();
    resetSession(); // 清空分析会话（草稿/任务状态），避免串用户
    // 清除会话内的分析结果，避免用户切换后看到上一个用户的报告
    try {
      window.sessionStorage.removeItem('resume-screening-result');
    } catch {
      // ignore
    }
    setUser(null);
    router.replace('/login');
  }, [router]);

  const value: AuthContextValue = {
    user,
    isAuthenticated: Boolean(user),
    isHydrated,
    isAdmin: Boolean(user?.is_admin),
    isSuperAdmin: Boolean(user?.is_super_admin),
    login,
    logout,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used within AuthProvider');
  return context;
}
