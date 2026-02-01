"use client";

import * as React from "react";
import { UploadCloud } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

interface DropZoneProps {
    onUpload: (file: File) => void;
}

export function DropZone({ onUpload }: DropZoneProps) {
    const [isDragging, setIsDragging] = React.useState(false);

    const handleDragOver = (e: React.DragEvent) => {
        e.preventDefault();
        setIsDragging(true);
    };

    const handleDragLeave = () => {
        setIsDragging(false);
    };

    const handleDrop = (e: React.DragEvent) => {
        e.preventDefault();
        setIsDragging(false);
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            onUpload(files[0]);
        }
    };

    const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        if (e.target.files && e.target.files.length > 0) {
            onUpload(e.target.files[0]);
        }
    };

    return (
        <Card
            className={`border-2 border-dashed transition-colors ${isDragging ? "border-primary bg-muted/50" : "border-muted-foreground/25"
                }`}
        >
            <CardContent
                className="flex flex-col items-center justify-center py-12 space-y-4"
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
            >
                <div className="p-4 bg-muted rounded-full">
                    <UploadCloud className="w-8 h-8 text-muted-foreground" />
                </div>
                <div className="text-center">
                    <h3 className="text-lg font-semibold">Drop Job Description PDF</h3>
                    <p className="text-sm text-muted-foreground">
                        Drag and drop your file here, or click to browse
                    </p>
                </div>
                <div className="relative">
                    <input
                        type="file"
                        className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
                        accept=".pdf"
                        onChange={handleFileChange}
                    />
                    <Button variant="secondary">Browse Files</Button>
                </div>
            </CardContent>
        </Card>
    );
}
