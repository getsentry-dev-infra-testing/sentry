from __future__ import annotations

from typing import Any

from django import forms

from sentry.models import Project
from sentry.services.hybrid_cloud.user.service import user_service


class MemberTeamForm(forms.Form):
    targetType = forms.ChoiceField()
    targetIdentifier = forms.CharField(
        required=False, help_text="Only required if 'Member' or 'Team' is selected"
    )
    teamValue = None
    memberValue = None
    targetTypeEnum = None

    def __init__(self, project, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project

    def clean_targetIdentifier(self):
        targetIdentifier = self.cleaned_data.get("targetIdentifier")
        # XXX: Clean up some bad data in the database
        if targetIdentifier == "None":
            targetIdentifier = None
        if targetIdentifier:
            try:
                targetIdentifier = int(targetIdentifier)
            except ValueError:
                raise forms.ValidationError("targetIdentifier must be an integer")
        return targetIdentifier

    def clean(self) -> dict[str, Any] | None:
        cleaned_data = super().clean()
        try:
            targetType = self.targetTypeEnum(cleaned_data.get("targetType"))
        except ValueError:
            msg = forms.ValidationError("Invalid targetType specified")
            self.add_error("targetType", msg)
            return

        targetIdentifier = cleaned_data.get("targetIdentifier")

        self.cleaned_data["targetType"] = targetType.value
        if targetType != self.teamValue and targetType != self.memberValue:
            return

        if not targetIdentifier:
            msg = forms.ValidationError("You need to specify a Team or Member.")
            self.add_error("targetIdentifier", msg)
            return

        if (
            targetType == self.teamValue
            and not Project.objects.filter(
                teams__id=int(targetIdentifier), id=self.project.id
            ).exists()
        ):
            msg = forms.ValidationError("This team is not part of the project.")
            self.add_error("targetIdentifier", msg)
            return

        if targetType == self.memberValue and not user_service.get_many(
            filter={
                "is_active": True,
                "is_active_memberteam": True,
                "organization_id": self.project.organization.id,
                "project_ids": [self.project.id],
                "user_ids": [int(targetIdentifier)],
            }
        ):
            msg = forms.ValidationError("This user is not part of the project.")
            self.add_error("targetIdentifier", msg)
            return

        self.cleaned_data["targetIdentifier"] = targetIdentifier
