# Playwright MCP Testing Framework for airecruiter

## Table of Contents
1. [Overview](#overview)
2. [Framework Setup](#framework-setup)
3. [Test Organization](#test-organization)
4. [Microsoft Login with Cookie Persistence](#microsoft-login-with-cookie-persistence)
5. [Complete Test Framework](#complete-test-framework)
6. [API Tests](#api-tests)
7. [Authentication Tests](#authentication-tests)
8. [Feature Tests](#feature-tests)
9. [Best Practices](#best-practices)
10. [Running Tests](#running-tests)

---

## Overview

### What Playwright MCP Should Test

A Playwright MCP (Model Context Protocol) server for the airecruiter project should handle:

1. **Authentication & Authorization**
   - Microsoft login flow
   - Cookie persistence and session management
   - Token refresh and expiration
   - Role-based access control (RBAC)
   - Permission validation

2. **User Interface Testing**
   - Page navigation and routing
   - Form validation and submission
   - Modal and dialog interactions
   - Component rendering and visibility
   - Error message display

3. **Feature-Specific Testing**
   - Candidate processing workflows
   - Job creation and management
   - Engagement workflows
   - Dashboard functionality
   - Pair dashboard operations
   - Analytics data display
   - DNC (Do Not Call) list management

4. **API Integration Testing**
   - API endpoint validation
   - Request/response validation
   - Error handling and status codes
   - Data persistence
   - Concurrent operations

5. **Performance Testing**
   - Page load times
   - API response times
   - Large dataset handling

6. **Accessibility Testing**
   - ARIA labels and roles
   - Keyboard navigation
   - Screen reader compatibility

---

## Framework Setup

### Prerequisites

```bash
npm install --save-dev @playwright/test
npm install --save-dev dotenv
npm install --save-dev @faker-js/faker
npm install --save-dev @testing-library/user-event
```

### Project Structure

```
tests/
├── fixtures/
│   ├── auth.fixture.ts
│   └── api.fixture.ts
├── e2e/
│   ├── auth.spec.ts
│   ├── dashboard.spec.ts
│   ├── candidates.spec.ts
│   ├── jobs.spec.ts
│   ├── engagement.spec.ts
│   └── admin.spec.ts
├── api/
│   ├── candidates.spec.ts
│   ├── jobs.spec.ts
│   └── admin.spec.ts
├── utils/
│   ├── auth.utils.ts
│   ├── cookies.utils.ts
│   ├── api.utils.ts
│   └── test-data.utils.ts
├── config/
│   ├── playwright.config.ts
│   └── test-config.env
└── auth-cookies/
    └── .gitkeep
```

### Playwright Configuration

**File: `tests/config/playwright.config.ts`**

```typescript
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL: 'https://qacurate.hoonr.ai',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] },
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
    },
  ],
});
```

---

## Test Organization

### Directory Structure Explanation

- **fixtures/**: Reusable test fixtures and setup logic
- **e2e/**: End-to-end UI tests
- **api/**: API integration tests
- **utils/**: Helper functions and utilities
- **config/**: Configuration files
- **auth-cookies/**: Stored authentication cookies for session persistence

---

## Microsoft Login with Cookie Persistence

### Strategy

1. Perform Microsoft login once
2. Save authentication cookies to a file
3. Reuse cookies in subsequent tests
4. Refresh session if cookies expire

### Implementation

**File: `tests/utils/cookies.utils.ts`**

```typescript
import fs from 'fs';
import path from 'path';
import { Page, BrowserContext } from '@playwright/test';

export class CookieManager {
  private cookiesFilePath: string;

  constructor(filename: string = 'auth-cookies.json') {
    this.cookiesFilePath = path.join(__dirname, '../auth-cookies', filename);
    this.ensureDirectory();
  }

  private ensureDirectory(): void {
    const dir = path.dirname(this.cookiesFilePath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
  }

  async saveCookies(context: BrowserContext): Promise<void> {
    const cookies = await context.cookies();
    fs.writeFileSync(this.cookiesFilePath, JSON.stringify(cookies, null, 2));
    console.log(`Cookies saved to ${this.cookiesFilePath}`);
  }

  async loadCookies(context: BrowserContext): Promise<void> {
    if (fs.existsSync(this.cookiesFilePath)) {
      const cookies = JSON.parse(fs.readFileSync(this.cookiesFilePath, 'utf-8'));
      await context.addCookies(cookies);
      console.log(`Cookies loaded from ${this.cookiesFilePath}`);
    }
  }

  clearCookies(): void {
    if (fs.existsSync(this.cookiesFilePath)) {
      fs.unlinkSync(this.cookiesFilePath);
      console.log('Cookies cleared');
    }
  }

  getCookiesFilePath(): string {
    return this.cookiesFilePath;
  }
}
```

### Authentication Utility

**File: `tests/utils/auth.utils.ts`**

```typescript
import { Page } from '@playwright/test';

export class MicrosoftAuthenticator {
  private page: Page;

  constructor(page: Page) {
    this.page = page;
  }

  async loginWithMicrosoft(
    email: string,
    password: string,
    baseUrl: string = 'https://qacurate.hoonr.ai'
  ): Promise<void> {
    // Navigate to login page
    await this.page.goto(baseUrl);
    
    // Wait for page to load
    await this.page.waitForLoadState('networkidle');

    // Click Microsoft login button
    const microsoftLoginButton = this.page.locator(
      'button:has-text("Login with Microsoft"), button:has-text("Sign in with Microsoft")'
    );
    
    if (await microsoftLoginButton.isVisible()) {
      await microsoftLoginButton.click();
    }

    // Wait for Microsoft login popup/redirect
    await this.page.waitForURL(/login\.microsoftonline\.com|auth\.microsoft\.com/, { timeout: 10000 });

    // Enter email
    const emailField = this.page.locator('input[type="email"], input[name="loginfmt"]');
    await emailField.fill(email);
    await this.page.locator('button:has-text("Next")').click();

    // Wait for password field
    await this.page.waitForSelector('input[type="password"], input[name="passwd"]', { timeout: 5000 });

    // Enter password
    const passwordField = this.page.locator('input[type="password"], input[name="passwd"]');
    await passwordField.fill(password);
    await this.page.locator('button:has-text("Sign in")').click();

    // Handle MFA if required
    await this.page.waitForNavigation({ waitUntil: 'networkidle', timeout: 10000 }).catch(() => {
      // MFA may be required
      return this.handleMFA();
    });

    // Wait for redirect back to application
    await this.page.waitForURL(baseUrl, { waitUntil: 'networkidle' });
  }

  private async handleMFA(): Promise<void> {
    // Check if MFA approval screen appears
    const approveButton = this.page.locator('button:has-text("Approve"), button:has-text("Yes")');
    
    if (await approveButton.isVisible()) {
      await approveButton.click();
      await this.page.waitForNavigation({ waitUntil: 'networkidle' });
    }
  }

  async isLoggedIn(): Promise<boolean> {
    try {
      // Check for logout button or user menu as indicator of logged-in state
      const userMenu = this.page.locator('[data-testid="user-menu"], button:has-text("Profile"), button:has-text("Sign out")');
      return await userMenu.isVisible({ timeout: 5000 });
    } catch {
      return false;
    }
  }
}
```

### Auth Fixture

**File: `tests/fixtures/auth.fixture.ts`**

```typescript
import { test as base, BrowserContext } from '@playwright/test';
import { CookieManager } from '../utils/cookies.utils';
import { MicrosoftAuthenticator } from '../utils/auth.utils';

type AuthFixtures = {
  authenticatedPage: any;
  cookieManager: CookieManager;
};

export const test = base.extend<AuthFixtures>({
  cookieManager: async ({}, use) => {
    const cookieManager = new CookieManager();
    await use(cookieManager);
  },

  authenticatedPage: async ({ page, cookieManager }, use) => {
    const cookieManager = new CookieManager();
    
    // Try to load existing cookies
    await cookieManager.loadCookies(page.context());
    
    // Navigate to app
    await page.goto('https://qacurate.hoonr.ai');
    
    // Check if already logged in
    const authenticator = new MicrosoftAuthenticator(page);
    const isLoggedIn = await authenticator.isLoggedIn();

    if (!isLoggedIn) {
      // Perform login
      const email = process.env.MICROSOFT_EMAIL || '';
      const password = process.env.MICROSOFT_PASSWORD || '';
      
      if (!email || !password) {
        throw new Error('MICROSOFT_EMAIL and MICROSOFT_PASSWORD environment variables are required');
      }

      await authenticator.loginWithMicrosoft(email, password);

      // Save cookies for future tests
      await cookieManager.saveCookies(page.context());
    }

    await use(page);
  },
});

export { expect } from '@playwright/test';
```

---

## Complete Test Framework

### Environment Configuration

**File: `tests/config/test-config.env`**

```env
# Microsoft Authentication
MICROSOFT_EMAIL=your-test-email@microsoft.com
MICROSOFT_PASSWORD=your-test-password
MICROSOFT_MFA_ENABLED=true

# Application URLs
BASE_URL=https://qacurate.hoonr.ai
API_BASE_URL=https://qacurate.hoonr.ai/api

# Test Data
TEST_TIMEOUT=30000
SLOW_MO=0

# Browser Configuration
HEADLESS=true
SCREENSHOT_ON_FAILURE=true
VIDEO_ON_FAILURE=true

# Parallel Execution
WORKERS=4
RETRIES=1
```

### Test Data Utilities

**File: `tests/utils/test-data.utils.ts`**

```typescript
import { faker } from '@faker-js/faker';

export class TestDataGenerator {
  static generateCandidate() {
    return {
      firstName: faker.person.firstName(),
      lastName: faker.person.lastName(),
      email: faker.internet.email(),
      phone: faker.phone.number('+1##########'),
      location: faker.location.city(),
      skills: [
        faker.word.words(3),
        faker.word.words(3),
        faker.word.words(3),
      ],
    };
  }

  static generateJob() {
    return {
      title: faker.job.title(),
      description: faker.lorem.paragraphs(2),
      location: faker.location.city(),
      salary: `${faker.number.int({ min: 50000, max: 150000 })}`,
      requirements: faker.lorem.sentences(5),
    };
  }

  static generateEngagementMessage() {
    return {
      subject: faker.lorem.sentence(),
      body: faker.lorem.paragraphs(2),
      recipientEmail: faker.internet.email(),
    };
  }

  static generateDNCEntry() {
    return {
      email: faker.internet.email(),
      phone: faker.phone.number('+1##########'),
      reason: faker.helpers.arrayElement(['No Interest', 'Unsubscribed', 'Do Not Contact']),
    };
  }
}
```

### API Test Utilities

**File: `tests/utils/api.utils.ts`**

```typescript
import { APIRequestContext } from '@playwright/test';

export class APIClient {
  private apiContext: APIRequestContext;
  private baseURL: string;
  private token?: string;

  constructor(apiContext: APIRequestContext, baseURL: string = 'https://qacurate.hoonr.ai/api') {
    this.apiContext = apiContext;
    this.baseURL = baseURL;
  }

  async setAuthToken(token: string): Promise<void> {
    this.token = token;
  }

  private getHeaders() {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (this.token) {
      headers['Authorization'] = `Bearer ${this.token}`;
    }
    return headers;
  }

  async get(endpoint: string) {
    return this.apiContext.get(`${this.baseURL}${endpoint}`, {
      headers: this.getHeaders(),
    });
  }

  async post(endpoint: string, data: any) {
    return this.apiContext.post(`${this.baseURL}${endpoint}`, {
      headers: this.getHeaders(),
      data,
    });
  }

  async put(endpoint: string, data: any) {
    return this.apiContext.put(`${this.baseURL}${endpoint}`, {
      headers: this.getHeaders(),
      data,
    });
  }

  async delete(endpoint: string) {
    return this.apiContext.delete(`${this.baseURL}${endpoint}`, {
      headers: this.getHeaders(),
    });
  }
}
```

---

## API Tests

### Candidate API Tests

**File: `tests/api/candidates.spec.ts`**

```typescript
import { test, expect } from '@playwright/test';
import { APIClient } from '../utils/api.utils';
import { TestDataGenerator } from '../utils/test-data.utils';

test.describe('Candidate API Tests', () => {
  let apiClient: APIClient;

  test.beforeAll(async ({ request }) => {
    apiClient = new APIClient(request);
  });

  test('Should retrieve all candidates', async () => {
    const response = await apiClient.get('/candidates');
    expect(response.status()).toBe(200);
    
    const data = await response.json();
    expect(Array.isArray(data.data || data)).toBeTruthy();
  });

  test('Should create a new candidate', async () => {
    const candidateData = TestDataGenerator.generateCandidate();
    const response = await apiClient.post('/candidates', candidateData);
    
    expect(response.status()).toBe(201);
    const data = await response.json();
    expect(data.email).toBe(candidateData.email);
  });

  test('Should update candidate information', async () => {
    const candidateData = TestDataGenerator.generateCandidate();
    const createResponse = await apiClient.post('/candidates', candidateData);
    const candidate = await createResponse.json();

    const updatedData = { ...candidateData, firstName: 'Updated' };
    const updateResponse = await apiClient.put(`/candidates/${candidate.id}`, updatedData);
    
    expect(updateResponse.status()).toBe(200);
  });

  test('Should search candidates by skills', async () => {
    const response = await apiClient.get('/candidates?skills=JavaScript');
    expect(response.status()).toBe(200);
  });

  test('Should delete a candidate', async () => {
    const candidateData = TestDataGenerator.generateCandidate();
    const createResponse = await apiClient.post('/candidates', candidateData);
    const candidate = await createResponse.json();

    const deleteResponse = await apiClient.delete(`/candidates/${candidate.id}`);
    expect(deleteResponse.status()).toBe(204);
  });
});
```

### Jobs API Tests

**File: `tests/api/jobs.spec.ts`**

```typescript
import { test, expect } from '@playwright/test';
import { APIClient } from '../utils/api.utils';
import { TestDataGenerator } from '../utils/test-data.utils';

test.describe('Jobs API Tests', () => {
  let apiClient: APIClient;

  test.beforeAll(async ({ request }) => {
    apiClient = new APIClient(request);
  });

  test('Should retrieve all jobs', async () => {
    const response = await apiClient.get('/jobs');
    expect(response.status()).toBe(200);
  });

  test('Should create a new job', async () => {
    const jobData = TestDataGenerator.generateJob();
    const response = await apiClient.post('/jobs', jobData);
    
    expect(response.status()).toBe(201);
    const data = await response.json();
    expect(data.title).toBe(jobData.title);
  });

  test('Should update job criteria', async () => {
    const jobData = TestDataGenerator.generateJob();
    const createResponse = await apiClient.post('/jobs', jobData);
    const job = await createResponse.json();

    const updatedCriteria = { requirements: 'Updated requirements' };
    const updateResponse = await apiClient.put(`/jobs/${job.id}`, updatedCriteria);
    
    expect(updateResponse.status()).toBe(200);
  });

  test('Should filter jobs by location', async () => {
    const response = await apiClient.get('/jobs?location=New%20York');
    expect(response.status()).toBe(200);
  });

  test('Should archive a job', async () => {
    const jobData = TestDataGenerator.generateJob();
    const createResponse = await apiClient.post('/jobs', jobData);
    const job = await createResponse.json();

    const archiveResponse = await apiClient.put(`/jobs/${job.id}/archive`, {});
    expect(archiveResponse.status()).toBe(200);
  });
});
```

---

## Authentication Tests

**File: `tests/e2e/auth.spec.ts`**

```typescript
import { test, expect } from '../fixtures/auth.fixture';

test.describe('Authentication Tests', () => {
  test('Should login with Microsoft credentials', async ({ authenticatedPage, cookieManager }) => {
    await authenticatedPage.goto('https://qacurate.hoonr.ai');
    
    // Verify user is logged in
    const userMenu = authenticatedPage.locator('[data-testid="user-menu"]');
    await expect(userMenu).toBeVisible();
  });

  test('Should persist session with saved cookies', async ({ page, cookieManager }) => {
    // Load cookies
    await cookieManager.loadCookies(page.context());
    
    // Navigate to app
    await page.goto('https://qacurate.hoonr.ai');
    
    // Should not require login
    const loginButton = page.locator('button:has-text("Login")');
    await expect(loginButton).not.toBeVisible({ timeout: 5000 });
  });

  test('Should logout successfully', async ({ authenticatedPage }) => {
    // Find logout button
    const logoutButton = authenticatedPage.locator('button:has-text("Sign out"), button:has-text("Logout")');
    
    if (await logoutButton.isVisible()) {
      await logoutButton.click();
      
      // Verify redirected to login
      const loginButton = authenticatedPage.locator('button:has-text("Login with Microsoft")');
      await expect(loginButton).toBeVisible();
    }
  });

  test('Should handle expired sessions', async ({ page }) => {
    // Clear all cookies to simulate expired session
    await page.context().clearCookies();
    
    // Navigate to protected page
    await page.goto('https://qacurate.hoonr.ai/dashboard');
    
    // Should redirect to login
    await expect(page).toHaveURL(/login|auth/);
  });

  test('Should handle RBAC permissions', async ({ authenticatedPage }) => {
    // Navigate to admin section
    await authenticatedPage.goto('https://qacurate.hoonr.ai/admin');
    
    // Check if user has access
    const adminPanel = authenticatedPage.locator('[data-testid="admin-panel"]');
    
    // Either panel is visible (has permission) or access denied message appears
    const hasAccess = await adminPanel.isVisible({ timeout: 5000 }).catch(() => false);
    const deniedMessage = authenticatedPage.locator('text=Access Denied');
    
    expect(hasAccess || await deniedMessage.isVisible()).toBeTruthy();
  });
});
```

---

## Feature Tests

### Dashboard Tests

**File: `tests/e2e/dashboard.spec.ts`**

```typescript
import { test, expect } from '../fixtures/auth.fixture';

test.describe('Dashboard Tests', () => {
  test.beforeEach(async ({ authenticatedPage }) => {
    await authenticatedPage.goto('https://qacurate.hoonr.ai/dashboard');
    await authenticatedPage.waitForLoadState('networkidle');
  });

  test('Should display dashboard overview', async ({ authenticatedPage }) => {
    // Check for key dashboard elements
    const dashboardTitle = authenticatedPage.locator('h1:has-text("Dashboard"), h1:has-text("Overview")');
    await expect(dashboardTitle).toBeVisible();
  });

  test('Should display candidate metrics', async ({ authenticatedPage }) => {
    const candidateCard = authenticatedPage.locator('[data-testid="candidate-metrics"]');
    await expect(candidateCard).toBeVisible();
  });

  test('Should display job metrics', async ({ authenticatedPage }) => {
    const jobCard = authenticatedPage.locator('[data-testid="job-metrics"]');
    await expect(jobCard).toBeVisible();
  });

  test('Should navigate to candidates page', async ({ authenticatedPage }) => {
    const candidatesLink = authenticatedPage.locator('a:has-text("Candidates"), button:has-text("Candidates")');
    await candidatesLink.click();
    
    await expect(authenticatedPage).toHaveURL(/candidates/);
  });

  test('Should navigate to jobs page', async ({ authenticatedPage }) => {
    const jobsLink = authenticatedPage.locator('a:has-text("Jobs"), button:has-text("Jobs")');
    await jobsLink.click();
    
    await expect(authenticatedPage).toHaveURL(/jobs/);
  });
});
```

### Candidate Processing Tests

**File: `tests/e2e/candidates.spec.ts`**

```typescript
import { test, expect } from '../fixtures/auth.fixture';
import { TestDataGenerator } from '../utils/test-data.utils';

test.describe('Candidate Processing Tests', () => {
  test.beforeEach(async ({ authenticatedPage }) => {
    await authenticatedPage.goto('https://qacurate.hoonr.ai/candidates');
    await authenticatedPage.waitForLoadState('networkidle');
  });

  test('Should display candidates list', async ({ authenticatedPage }) => {
    const candidatesList = authenticatedPage.locator('[data-testid="candidates-list"]');
    await expect(candidatesList).toBeVisible();
  });

  test('Should add a new candidate', async ({ authenticatedPage }) => {
    const addButton = authenticatedPage.locator('button:has-text("Add Candidate"), button:has-text("New Candidate")');
    await addButton.click();

    // Fill form
    const candidateData = TestDataGenerator.generateCandidate();
    await authenticatedPage.locator('input[placeholder*="First Name"]').fill(candidateData.firstName);
    await authenticatedPage.locator('input[placeholder*="Last Name"]').fill(candidateData.lastName);
    await authenticatedPage.locator('input[type="email"]').fill(candidateData.email);
    await authenticatedPage.locator('input[placeholder*="Phone"]').fill(candidateData.phone);

    // Submit
    const submitButton = authenticatedPage.locator('button:has-text("Submit"), button:has-text("Save")');
    await submitButton.click();

    // Verify success message
    const successMessage = authenticatedPage.locator('text=Success, text=added successfully');
    await expect(successMessage).toBeVisible();
  });

  test('Should search candidates', async ({ authenticatedPage }) => {
    const searchInput = authenticatedPage.locator('input[placeholder*="Search"]');
    await searchInput.fill('John');

    await authenticatedPage.waitForLoadState('networkidle');

    const results = authenticatedPage.locator('[data-testid="candidates-list"] >> text=John');
    await expect(results).toBeVisible();
  });

  test('Should filter candidates by status', async ({ authenticatedPage }) => {
    const filterButton = authenticatedPage.locator('button:has-text("Filter")');
    await filterButton.click();

    const statusFilter = authenticatedPage.locator('select[name="status"], label:has-text("Status")');
    await statusFilter.click();

    const option = authenticatedPage.locator('text=Active');
    await option.click();

    await authenticatedPage.waitForLoadState('networkidle');
  });

  test('Should view candidate details', async ({ authenticatedPage }) => {
    const firstCandidate = authenticatedPage.locator('[data-testid="candidate-row"]').first();
    await firstCandidate.click();

    await expect(authenticatedPage).toHaveURL(/candidates\/\w+/);

    const detailsPanel = authenticatedPage.locator('[data-testid="candidate-details"]');
    await expect(detailsPanel).toBeVisible();
  });

  test('Should delete a candidate', async ({ authenticatedPage }) => {
    const firstCandidate = authenticatedPage.locator('[data-testid="candidate-row"]').first();
    await firstCandidate.hover();

    const deleteButton = firstCandidate.locator('button[aria-label="Delete"]');
    await deleteButton.click();

    // Confirm deletion
    const confirmButton = authenticatedPage.locator('button:has-text("Confirm"), button:has-text("Delete")');
    await confirmButton.click();

    const successMessage = authenticatedPage.locator('text=Deleted successfully');
    await expect(successMessage).toBeVisible();
  });
});
```

### Job Management Tests

**File: `tests/e2e/jobs.spec.ts`**

```typescript
import { test, expect } from '../fixtures/auth.fixture';
import { TestDataGenerator } from '../utils/test-data.utils';

test.describe('Job Management Tests', () => {
  test.beforeEach(async ({ authenticatedPage }) => {
    await authenticatedPage.goto('https://qacurate.hoonr.ai/jobs');
    await authenticatedPage.waitForLoadState('networkidle');
  });

  test('Should display jobs list', async ({ authenticatedPage }) => {
    const jobsList = authenticatedPage.locator('[data-testid="jobs-list"]');
    await expect(jobsList).toBeVisible();
  });

  test('Should create a new job', async ({ authenticatedPage }) => {
    const addButton = authenticatedPage.locator('button:has-text("Create Job"), button:has-text("New Job")');
    await addButton.click();

    const jobData = TestDataGenerator.generateJob();

    await authenticatedPage.locator('input[placeholder*="Job Title"]').fill(jobData.title);
    await authenticatedPage.locator('textarea[placeholder*="Description"]').fill(jobData.description);
    await authenticatedPage.locator('input[placeholder*="Location"]').fill(jobData.location);

    const submitButton = authenticatedPage.locator('button:has-text("Create"), button:has-text("Save")');
    await submitButton.click();

    const successMessage = authenticatedPage.locator('text=Job created, text=successfully');
    await expect(successMessage).toBeVisible();
  });

  test('Should edit job criteria', async ({ authenticatedPage }) => {
    const firstJob = authenticatedPage.locator('[data-testid="job-row"]').first();
    await firstJob.hover();

    const editButton = firstJob.locator('button[aria-label="Edit"]');
    await editButton.click();

    const titleInput = authenticatedPage.locator('input[placeholder*="Job Title"]');
    await titleInput.fill('Updated Job Title');

    const saveButton = authenticatedPage.locator('button:has-text("Save")');
    await saveButton.click();

    const successMessage = authenticatedPage.locator('text=Updated successfully');
    await expect(successMessage).toBeVisible();
  });

  test('Should archive a job', async ({ authenticatedPage }) => {
    const firstJob = authenticatedPage.locator('[data-testid="job-row"]').first();
    await firstJob.hover();

    const menuButton = firstJob.locator('button[aria-label="More"]');
    await menuButton.click();

    const archiveOption = authenticatedPage.locator('text=Archive');
    await archiveOption.click();

    const confirmButton = authenticatedPage.locator('button:has-text("Confirm")');
    await confirmButton.click();

    const successMessage = authenticatedPage.locator('text=Archived successfully');
    await expect(successMessage).toBeVisible();
  });
});
```

### Engagement Tests

**File: `tests/e2e/engagement.spec.ts`**

```typescript
import { test, expect } from '../fixtures/auth.fixture';
import { TestDataGenerator } from '../utils/test-data.utils';

test.describe('Engagement Workflow Tests', () => {
  test.beforeEach(async ({ authenticatedPage }) => {
    await authenticatedPage.goto('https://qacurate.hoonr.ai/engagement');
    await authenticatedPage.waitForLoadState('networkidle');
  });

  test('Should display engagement dashboard', async ({ authenticatedPage }) => {
    const engagementDashboard = authenticatedPage.locator('[data-testid="engagement-dashboard"]');
    await expect(engagementDashboard).toBeVisible();
  });

  test('Should create engagement message', async ({ authenticatedPage }) => {
    const createButton = authenticatedPage.locator('button:has-text("New Message"), button:has-text("Compose")');
    await createButton.click();

    const messageData = TestDataGenerator.generateEngagementMessage();

    await authenticatedPage.locator('input[placeholder*="Subject"]').fill(messageData.subject);
    await authenticatedPage.locator('textarea[placeholder*="Message"]').fill(messageData.body);
    await authenticatedPage.locator('input[placeholder*="Email"]').fill(messageData.recipientEmail);

    const sendButton = authenticatedPage.locator('button:has-text("Send")');
    await sendButton.click();

    const successMessage = authenticatedPage.locator('text=Message sent, text=successfully');
    await expect(successMessage).toBeVisible();
  });

  test('Should view engagement analytics', async ({ authenticatedPage }) => {
    const analyticsLink = authenticatedPage.locator('a:has-text("Analytics"), button:has-text("Analytics")');
    await analyticsLink.click();

    const analyticsPanel = authenticatedPage.locator('[data-testid="analytics-panel"]');
    await expect(analyticsPanel).toBeVisible();
  });

  test('Should schedule engagement campaign', async ({ authenticatedPage }) => {
    const scheduleButton = authenticatedPage.locator('button:has-text("Schedule Campaign")');
    await scheduleButton.click();

    // Select campaign template
    const templateSelect = authenticatedPage.locator('select[name="template"]');
    await templateSelect.click();
    await authenticatedPage.locator('text=Welcome Series').click();

    // Set schedule
    const dateInput = authenticatedPage.locator('input[type="date"]');
    const tomorrow = new Date();
    tomorrow.setDate(tomorrow.getDate() + 1);
    await dateInput.fill(tomorrow.toISOString().split('T')[0]);

    const confirmButton = authenticatedPage.locator('button:has-text("Schedule")');
    await confirmButton.click();

    const successMessage = authenticatedPage.locator('text=Campaign scheduled');
    await expect(successMessage).toBeVisible();
  });
});
```

### Admin & Analytics Tests

**File: `tests/e2e/admin.spec.ts`**

```typescript
import { test, expect } from '../fixtures/auth.fixture';

test.describe('Admin & Analytics Tests', () => {
  test.beforeEach(async ({ authenticatedPage }) => {
    await authenticatedPage.goto('https://qacurate.hoonr.ai/admin');
    await authenticatedPage.waitForLoadState('networkidle');
  });

  test('Should display admin dashboard', async ({ authenticatedPage }) => {
    const adminDashboard = authenticatedPage.locator('[data-testid="admin-dashboard"]');
    await expect(adminDashboard).toBeVisible();
  });

  test('Should manage DNC list', async ({ authenticatedPage }) => {
    const dncLink = authenticatedPage.locator('a:has-text("DNC List"), a:has-text("Do Not Contact")');
    await dncLink.click();

    const dncTable = authenticatedPage.locator('[data-testid="dnc-table"]');
    await expect(dncTable).toBeVisible();
  });

  test('Should add entry to DNC list', async ({ authenticatedPage }) => {
    const dncLink = authenticatedPage.locator('a:has-text("DNC List")');
    await dncLink.click();

    const addButton = authenticatedPage.locator('button:has-text("Add Entry")');
    await addButton.click();

    await authenticatedPage.locator('input[placeholder*="Email"]').fill('test@example.com');
    await authenticatedPage.locator('input[placeholder*="Phone"]').fill('+1234567890');

    const submitButton = authenticatedPage.locator('button:has-text("Add")');
    await submitButton.click();

    const successMessage = authenticatedPage.locator('text=Entry added successfully');
    await expect(successMessage).toBeVisible();
  });

  test('Should view analytics dashboard', async ({ authenticatedPage }) => {
    const analyticsLink = authenticatedPage.locator('a:has-text("Analytics")');
    await analyticsLink.click();

    const analyticsDashboard = authenticatedPage.locator('[data-testid="analytics-dashboard"]');
    await expect(analyticsDashboard).toBeVisible();
  });

  test('Should export analytics report', async ({ authenticatedPage }) => {
    const exportButton = authenticatedPage.locator('button:has-text("Export")');
    await exportButton.click();

    const csvOption = authenticatedPage.locator('text=Export as CSV');
    await csvOption.click();

    // Wait for download
    const downloadPromise = authenticatedPage.context().waitForEvent('download');
    await downloadPromise;
  });

  test('Should manage user roles', async ({ authenticatedPage }) => {
    const usersLink = authenticatedPage.locator('a:has-text("Users"), a:has-text("Team Members")');
    await usersLink.click();

    const usersTable = authenticatedPage.locator('[data-testid="users-table"]');
    await expect(usersTable).toBeVisible();
  });

  test('Should update user permissions', async ({ authenticatedPage }) => {
    const usersLink = authenticatedPage.locator('a:has-text("Users")');
    await usersLink.click();

    const firstUser = authenticatedPage.locator('[data-testid="user-row"]').first();
    const roleSelect = firstUser.locator('select[name="role"]');

    await roleSelect.selectOption('Manager');
    
    const saveButton = authenticatedPage.locator('button:has-text("Save")');
    await saveButton.click();

    const successMessage = authenticatedPage.locator('text=Permissions updated');
    await expect(successMessage).toBeVisible();
  });
});
```

---

## Best Practices

### 1. Test Organization
- **One concern per test**: Each test should validate a single piece of functionality
- **Meaningful names**: Test names should clearly describe what is being tested
- **Setup and teardown**: Use `beforeEach` and `afterEach` appropriately
- **Avoid test interdependence**: Tests should be able to run in any order

### 2. Selectors
```typescript
// Good: Use data-testid
page.locator('[data-testid="submit-button"]')

// Good: Use role and name
page.locator('button:has-text("Submit")')
page.getByRole('button', { name: 'Submit' })

// Avoid: Class-based selectors (brittle)
page.locator('.btn.btn-primary')

// Avoid: XPath (hard to maintain)
page.locator('//*[@id="form"]/div[1]/button')
```

### 3. Wait Strategies
```typescript
// Good: Wait for specific element
await page.waitForSelector('[data-testid="success-message"]')
await expect(element).toBeVisible()

// Good: Wait for navigation
await page.waitForNavigation({ waitUntil: 'networkidle' })
await expect(page).toHaveURL(/expected-url/)

// Avoid: Arbitrary sleep
await page.waitForTimeout(5000) // Don't use unless absolutely necessary
```

### 4. Assertions
```typescript
// Good: Specific assertions
await expect(element).toBeVisible()
await expect(element).toHaveText('Expected Text')
await expect(element).toHaveAttribute('disabled')
await expect(page).toHaveURL('https://qacurate.hoonr.ai/dashboard')

// Avoid: Generic assertions
expect(element).toBeTruthy()
expect(text).not.toBe('')
```

### 5. Error Handling
```typescript
// Handle optional elements
const element = page.locator('selector');
const isVisible = await element.isVisible().catch(() => false);

// Retry logic for flaky operations
for (let i = 0; i < 3; i++) {
  try {
    await page.locator('button').click();
    break;
  } catch (e) {
    if (i === 2) throw e;
    await page.waitForTimeout(1000);
  }
}
```

### 6. Environment Configuration
```typescript
// Load environment variables
require('dotenv').config({ path: './tests/config/test-config.env' });

// Use in tests
const baseUrl = process.env.BASE_URL || 'https://qacurate.hoonr.ai';
const timeout = parseInt(process.env.TEST_TIMEOUT || '30000', 10);
```

### 7. Cookie Management Best Practices
- Save cookies after successful login
- Add `.gitignore` entry for auth-cookies folder
- Refresh cookies periodically (daily/weekly)
- Clear cookies between different test suites if needed
- Log cookie expiration times for monitoring

---

## Running Tests

### Setup

1. **Install dependencies:**
```bash
npm install
```

2. **Configure environment variables:**
Create `.env` file in project root:
```env
MICROSOFT_EMAIL=your-email@microsoft.com
MICROSOFT_PASSWORD=your-password
BASE_URL=https://qacurate.hoonr.ai
```

3. **Add .gitignore entry:**
```
tests/auth-cookies/
.env
test-results/
```

### Run All Tests
```bash
npx playwright test
```

### Run Specific Test File
```bash
npx playwright test tests/e2e/auth.spec.ts
```

### Run Tests with Specific Tag
```bash
npx playwright test --grep @smoke
```

### Run Tests in UI Mode (Recommended for Debugging)
```bash
npx playwright test --ui
```

### Run Tests in Debug Mode
```bash
npx playwright test --debug
```

### Run Tests in Headed Mode (See Browser)
```bash
npx playwright test --headed
```

### Generate HTML Report
```bash
npx playwright show-report
```

### Run Tests in Parallel
```bash
npx playwright test --workers=4
```

### Run Tests Sequentially
```bash
npx playwright test --workers=1
```

### Update Snapshots
```bash
npx playwright test --update-snapshots
```

### Record New Tests
```bash
npx playwright codegen https://qacurate.hoonr.ai
```

---

## CI/CD Integration

### GitHub Actions Example

**File: `.github/workflows/e2e-tests.yml`**

```yaml
name: E2E Tests

on: [push, pull_request]

jobs:
  test:
    timeout-minutes: 60
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-node@v3
        with:
          node-version: 18
      
      - name: Install dependencies
        run: npm install
      
      - name: Install Playwright
        run: npx playwright install --with-deps
      
      - name: Run tests
        env:
          MICROSOFT_EMAIL: ${{ secrets.MICROSOFT_EMAIL }}
          MICROSOFT_PASSWORD: ${{ secrets.MICROSOFT_PASSWORD }}
        run: npx playwright test
      
      - name: Upload test results
        if: always()
        uses: actions/upload-artifact@v3
        with:
          name: playwright-report
          path: playwright-report/
          retention-days: 30
```

---

## Troubleshooting

### Common Issues

**1. Microsoft Login Timeout**
```typescript
// Increase timeout
await page.waitForURL(/login\.microsoftonline\.com/, { timeout: 20000 });
```

**2. Cookie Expires**
```typescript
// Implement cookie refresh
if (cookieAge > 24 * 60 * 60 * 1000) { // 24 hours
  cookieManager.clearCookies();
  await authenticator.loginWithMicrosoft(email, password);
}
```

**3. MFA Blocking Tests**
```typescript
// Disable MFA for test account or handle in code
// Alternatively, use app passwords if available
```

**4. Element Not Found**
```typescript
// Increase wait time
await expect(element).toBeVisible({ timeout: 10000 });
```

**5. Network Timeouts**
```typescript
// Adjust network timeout
test.setTimeout(60000); // 60 seconds
```

---

## Performance Benchmarks

Expected test execution times (on standard hardware):

- Authentication test: 2-5 minutes (first time), 30-60 seconds (with cookies)
- Single feature test: 30-60 seconds
- Full test suite: 10-15 minutes (parallel execution)
- Full test suite: 30-45 minutes (sequential execution)

---

## References

- [Playwright Documentation](https://playwright.dev)
- [Microsoft Authentication](https://learn.microsoft.com/en-us/azure/active-directory/develop/quickstart-web-app-nodejs-express)
- [Cookie Management](https://playwright.dev/docs/api/class-browsercontext#browser-context-add-cookies)
- [Best Practices](https://playwright.dev/docs/best-practices)

---

## Contact & Support

For questions or issues with this testing framework, please refer to the project documentation or reach out to the QA team.

**Last Updated:** 2026-07-16
**Base Application URL:** https://qacurate.hoonr.ai
**Framework Version:** 1.0
