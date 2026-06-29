'use client';

import { Copy, LayoutTemplate, Loader2 } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import {
    duplicateWorkflowTemplateApiV1WorkflowTemplatesDuplicatePost,
    getWorkflowTemplatesApiV1WorkflowTemplatesGet,
} from '@/client/sdk.gen';
import type { WorkflowTemplateResponse } from '@/client/types.gen';
import { Button } from '@/components/ui/button';
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogHeader,
    DialogTitle,
} from '@/components/ui/dialog';
import { useAuth } from '@/lib/auth';
import logger from '@/lib/logger';

export function TemplateGalleryDialog({
    open,
    onOpenChange,
}: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
}) {
    const router = useRouter();
    const { getAccessToken } = useAuth();
    const [templates, setTemplates] = useState<WorkflowTemplateResponse[]>([]);
    const [loading, setLoading] = useState(false);
    const [creatingId, setCreatingId] = useState<number | null>(null);

    useEffect(() => {
        if (!open) return;
        let cancelled = false;

        const load = async () => {
            setLoading(true);
            try {
                const token = await getAccessToken();
                const response = await getWorkflowTemplatesApiV1WorkflowTemplatesGet({
                    headers: { Authorization: `Bearer ${token}` },
                });
                if (!cancelled && response.data) {
                    setTemplates(response.data as WorkflowTemplateResponse[]);
                }
            } catch (err) {
                logger.error(`Failed to load workflow templates: ${err}`);
                if (!cancelled) toast.error('Failed to load templates');
            } finally {
                if (!cancelled) setLoading(false);
            }
        };

        void load();
        return () => {
            cancelled = true;
        };
    }, [open, getAccessToken]);

    const handleUse = async (template: WorkflowTemplateResponse) => {
        setCreatingId(template.id);
        try {
            const token = await getAccessToken();
            const response =
                await duplicateWorkflowTemplateApiV1WorkflowTemplatesDuplicatePost({
                    body: {
                        template_id: template.id,
                        workflow_name: template.template_name,
                    },
                    headers: {
                        Authorization: `Bearer ${token}`,
                        'Content-Type': 'application/json',
                    },
                });
            if (response.error) throw new Error('Template duplication failed');
            if (response.data?.id) {
                router.push(`/workflow/${response.data.id}`);
                return;
            }
            throw new Error('No workflow id returned');
        } catch (err) {
            logger.error(`Failed to create agent from template: ${err}`);
            toast.error('Failed to create agent from template');
            setCreatingId(null);
        }
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
                <DialogHeader>
                    <DialogTitle>Start from a template</DialogTitle>
                    <DialogDescription>
                        Pick a ready-made voice agent to get started fast. You can
                        customize everything in the editor afterwards.
                    </DialogDescription>
                </DialogHeader>

                {loading ? (
                    <div className="flex min-h-40 items-center justify-center">
                        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
                    </div>
                ) : templates.length === 0 ? (
                    <div className="flex min-h-40 items-center justify-center text-sm text-muted-foreground">
                        No templates available yet.
                    </div>
                ) : (
                    <div className="grid gap-3 sm:grid-cols-2">
                        {templates.map((template) => (
                            <div
                                key={template.id}
                                className="flex flex-col justify-between rounded-lg border p-4"
                            >
                                <div>
                                    <div className="flex items-center gap-2 font-semibold">
                                        <LayoutTemplate className="h-4 w-4 shrink-0 text-primary" />
                                        {template.template_name}
                                    </div>
                                    <p className="mt-1 text-sm text-muted-foreground">
                                        {template.template_description}
                                    </p>
                                </div>
                                <Button
                                    className="mt-4 w-full"
                                    variant="outline"
                                    disabled={creatingId !== null}
                                    onClick={() => handleUse(template)}
                                >
                                    {creatingId === template.id ? (
                                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                                    ) : (
                                        <Copy className="h-4 w-4 mr-2" />
                                    )}
                                    Use this template
                                </Button>
                            </div>
                        ))}
                    </div>
                )}
            </DialogContent>
        </Dialog>
    );
}
