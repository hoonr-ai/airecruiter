const FREE_EMAIL_DOMAINS = new Set([
  "gmail.com",
  "yahoo.com",
  "hotmail.com",
  "outlook.com",
  "aol.com",
  "icloud.com",
  "mail.com",
  "protonmail.com",
  "yandex.com",
  "zoho.com",
  "gmx.com",
  "live.com",
  "msn.com",
  "me.com",
  "mac.com",
  "proton.me",
  "gmx.net",
  "fastmail.com",
  "qq.com",
  "163.com",
  "rediffmail.com",
  "ymail.com",
  "yahoo.co.uk",
  "hotmail.co.uk",
  "live.co.uk",
  "googlemail.com",
]);

export function isWorkEmail(email: string | null | undefined): boolean {
  if (!email || !email.includes("@")) {
    return false;
  }
  
  const normalized = email.trim().toLowerCase();
  const parts = normalized.split("@");
  const domain = parts[parts.length - 1];
  
  return !FREE_EMAIL_DOMAINS.has(domain);
}
