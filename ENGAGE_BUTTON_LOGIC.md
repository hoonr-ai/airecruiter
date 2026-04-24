# Engage Button Logic: Complete Guide

## 1. Overview

The Engage button allows users to preview and edit a JSON payload before sending it to a bulk interview API. The workflow is:
- User clicks Engage.
- A modal opens, showing the payload (editable).
- User can edit and confirm.
- On confirmation, the payload is sent to the API.

---

## 2. State Management

Add the following state hooks inside your main component (e.g., in candidates/page.tsx):

```tsx
const [isEngageModalOpen, setIsEngageModalOpen] = useState(false);
const [engagePayload, setEngagePayload] = useState<string>('');
const [engageLoading, setEngageLoading] = useState(false);
const [engageError, setEngageError] = useState<string | null>(null);
```

---

## 3. Generate the Payload

Create a function to generate the payload for the selected candidates:

```tsx
function generateEngagePayload(selectedCandidates: Candidate[]): string {
  const payload = {
    candidates: selectedCandidates.map((c) => ({
      id: c.id,
      name: c.name,
      email: c.email,
      // ...other fields as needed
    })),
    // ...other payload fields
  };
  return JSON.stringify(payload, null, 2);
}
```

---

## 4. Open the Modal

When the Engage button is clicked, generate the payload and open the modal:

```tsx
const handleEngageClick = () => {
  const payload = generateEngagePayload(selectedCandidates);
  setEngagePayload(payload);
  setIsEngageModalOpen(true);
};
```

---

## 5. Modal UI

Use your Dialog/Modal component to show the payload and allow editing:

```tsx
<Dialog open={isEngageModalOpen} onOpenChange={setIsEngageModalOpen}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>Preview & Edit Engage Payload</DialogTitle>
    </DialogHeader>
    <Textarea
      value={engagePayload}
      onChange={(e) => setEngagePayload(e.target.value)}
      rows={12}
    />
    {engageError && <div className="text-red-500">{engageError}</div>}
    <DialogFooter>
      <Button onClick={handleScheduleCall} disabled={engageLoading}>
        {engageLoading ? 'Sending...' : 'Send'}
      </Button>
      <Button variant="secondary" onClick={() => setIsEngageModalOpen(false)}>
        Cancel
      </Button>
    </DialogFooter>
  </DialogContent>
</Dialog>
```

---

## 6. Send the API Request

Handle the API call when the user confirms:

```tsx
const handleScheduleCall = async () => {
  setEngageLoading(true);
  setEngageError(null);
  try {
    const response = await fetch('http://ec2-13-62-55-80.eu-north-1.compute.amazonaws.com/api/bulk-interviews', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: engagePayload,
    });
    if (!response.ok) throw new Error('Failed to send payload');
    setIsEngageModalOpen(false);
    // Optionally show a success message
  } catch (err: any) {
    setEngageError(err.message || 'Unknown error');
  } finally {
    setEngageLoading(false);
  }
};
```

---

## 7. Engage Button in Table

Add the Engage button to your UI, e.g., above the candidate table:

```tsx
<Button onClick={handleEngageClick} disabled={selectedCandidates.length === 0}>
  Engage
</Button>
```

---

## 8. Full Example Snippet

Here’s how the main parts fit together inside your component:

```tsx
// ...imports...

export default function CandidatesPage() {
  // ...other state...
  const [isEngageModalOpen, setIsEngageModalOpen] = useState(false);
  const [engagePayload, setEngagePayload] = useState<string>('');
  const [engageLoading, setEngageLoading] = useState(false);
  const [engageError, setEngageError] = useState<string | null>(null);

  const handleEngageClick = () => {
    const payload = generateEngagePayload(selectedCandidates);
    setEngagePayload(payload);
    setIsEngageModalOpen(true);
  };

  const handleScheduleCall = async () => {
    setEngageLoading(true);
    setEngageError(null);
    try {
      const response = await fetch('http://ec2-13-62-55-80.eu-north-1.compute.amazonaws.com/api/bulk-interviews', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: engagePayload,
      });
      if (!response.ok) throw new Error('Failed to send payload');
      setIsEngageModalOpen(false);
    } catch (err: any) {
      setEngageError(err.message || 'Unknown error');
    } finally {
      setEngageLoading(false);
    }
  };

  return (
    <>
      <Button onClick={handleEngageClick} disabled={selectedCandidates.length === 0}>
        Engage
      </Button>
      {/* ...candidate table... */}
      <Dialog open={isEngageModalOpen} onOpenChange={setIsEngageModalOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Preview & Edit Engage Payload</DialogTitle>
          </DialogHeader>
          <Textarea
            value={engagePayload}
            onChange={(e) => setEngagePayload(e.target.value)}
            rows={12}
          />
          {engageError && <div className="text-red-500">{engageError}</div>}
          <DialogFooter>
            <Button onClick={handleScheduleCall} disabled={engageLoading}>
              {engageLoading ? 'Sending...' : 'Send'}
            </Button>
            <Button variant="secondary" onClick={() => setIsEngageModalOpen(false)}>
              Cancel
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
```

---

## 9. Notes

- Ensure all hooks are inside the component.
- Import all required UI components.
- Adjust the payload structure as needed for your API.

---

Let your team know to refer to this document for implementing or maintaining the Engage button logic.
