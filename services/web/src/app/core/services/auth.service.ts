import { inject, Injectable, signal } from '@angular/core';
import { Router } from '@angular/router';

const STORAGE_KEY = 'visualizerBasicAuth';

interface StoredCredentials {
  username: string;
  token: string;
}

@Injectable({
  providedIn: 'root'
})
export class AuthService {
  private readonly router = inject(Router);

  private readonly credentialsSignal = signal<StoredCredentials | null>(
    this.readFromStorage()
  );

  readonly isAuthenticated = signal<boolean>(!!this.credentialsSignal());

  login(username: string, password: string): void {
    const token = btoa(`${username}:${password}`);
    const payload: StoredCredentials = { username, token };
    this.writeToStorage(payload);
    this.credentialsSignal.set(payload);
    this.isAuthenticated.set(true);
  }

  logout(): void {
    this.clearStorage();
    this.credentialsSignal.set(null);
    this.isAuthenticated.set(false);
    void this.router.navigate(['/login']);
  }

  getAuthorizationHeader(): string | null {
    const value = this.credentialsSignal();
    if (!value) {
      return null;
    }
    return `Basic ${value.token}`;
  }

  private readFromStorage(): StoredCredentials | null {
    if (typeof localStorage === 'undefined') {
      return null;
    }
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return null;
    }
    try {
      return JSON.parse(raw) as StoredCredentials;
    } catch {
      return null;
    }
  }

  private writeToStorage(value: StoredCredentials): void {
    if (typeof localStorage === 'undefined') {
      return;
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  }

  private clearStorage(): void {
    if (typeof localStorage === 'undefined') {
      return;
    }
    localStorage.removeItem(STORAGE_KEY);
  }
}
