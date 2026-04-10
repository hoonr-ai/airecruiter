"use client";

import { useState } from "react";
import { 
  Dialog, 
  DialogContent, 
  DialogHeader, 
  DialogTitle 
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Mail, Send } from "lucide-react";

interface CandidateMessageModalProps {
  candidateName: string;
  candidateEmail: string;
  isOpen: boolean;
  onClose: () => void;
  onSendMessage?: (message: string) => void;
}

export function CandidateMessageModal({ 
  candidateName, 
  candidateEmail,
  isOpen, 
  onClose,
  onSendMessage
}: CandidateMessageModalProps) {
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);

  // Generate default message
  const defaultMessage = `Hi ${candidateName.split(' ')[0]},

I hope this email finds you well.

I reviewed your profile in our database and thought you'd be a great fit for a new opportunity we have.

Would you be interested in learning more about this position? I'd love to schedule a brief call to discuss the details.

Best regards,
Recruiting Team`;

  const handleSendMessage = async () => {
    if (!message.trim()) return;
    
    setSending(true);
    
    try {
      // Send message via API
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/candidates/message`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          candidate_provider_id: candidateEmail,
          message: message,
          source: "JobDiva"
        })
      });
      
      if (response.ok) {
        console.log("Message sent successfully");
        // Also open mailto as backup/actual sending mechanism
        const subject = "Exciting Opportunity - Let's Connect";
        const mailtoUrl = `mailto:${candidateEmail}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(message)}`;
        window.open(mailtoUrl, "_blank");
      } else {
        throw new Error("Failed to send message");
      }
      
      // Close modal and reset
      onClose();
      setMessage("");
    } catch (error) {
      console.error("Error sending message:", error);
      // Fallback to mailto only
      const subject = "Exciting Opportunity - Let's Connect"; 
      const mailtoUrl = `mailto:${candidateEmail}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(message)}`;
      window.open(mailtoUrl, "_blank");
      onClose();
      setMessage("");
    } finally {
      setSending(false);
    }
  };

  const handleClose = () => {
    setMessage("");
    onClose();
  };

  // Set default message when modal opens
  if (isOpen && !message) {
    setMessage(defaultMessage);
  }

  return (
    <Dialog open={isOpen} onOpenChange={handleClose}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center space-x-2">
            <Mail className="w-5 h-5" />
            <span>Message {candidateName}</span>
          </DialogTitle>
          <p className="text-sm text-muted-foreground">
            Customize your invitation message below.
          </p>
        </DialogHeader>

        <div className="space-y-4">
          <div>
            <Label htmlFor="message">Message</Label>
            <Textarea
              id="message"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              placeholder="Enter your message..."
              rows={10}
              className="mt-2"
            />
          </div>

          <div className="text-xs text-muted-foreground">
            <p><strong>To:</strong> {candidateEmail}</p>
            <p><strong>Subject:</strong> Exciting Opportunity - Let's Connect</p>
          </div>

          <div className="flex justify-end space-x-3">
            <Button variant="outline" onClick={handleClose} disabled={sending}>
              Cancel
            </Button>
            <Button 
              onClick={handleSendMessage} 
              disabled={!message.trim() || sending}
              className="bg-blue-600 hover:bg-blue-700"
            >
              {sending ? (
                "Sending..."
              ) : (
                <>
                  <Send className="w-4 h-4 mr-2" />
                  Send Message
                </>
              )}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}