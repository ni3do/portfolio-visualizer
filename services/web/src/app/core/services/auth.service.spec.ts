import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';

import { AuthService } from './auth.service';

describe('AuthService', () => {
  let service: AuthService;
  const navigateSpy = jasmine.createSpy('navigate');

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        AuthService,
        { provide: Router, useValue: { navigate: navigateSpy } }
      ]
    });
    service = TestBed.inject(AuthService);
    if (typeof localStorage !== 'undefined') {
      localStorage.clear();
    }
    navigateSpy.calls.reset();
  });

  it('stores credentials on login', () => {
    service.login('user', 'secret');

    expect(service.isAuthenticated()).toBeTrue();
    expect(service.getAuthorizationHeader()).toContain('Basic');
  });

  it('clears credentials on logout', () => {
    service.login('user', 'secret');
    service.logout();

    expect(service.isAuthenticated()).toBeFalse();
    expect(service.getAuthorizationHeader()).toBeNull();
    expect(navigateSpy).toHaveBeenCalled();
  });
});
