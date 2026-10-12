import logging
import re

from django.conf import settings
from django.core.mail import send_mail
from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login
from django.db import transaction

from .models import (
    Project,
    ConsumptionMode,
    AnalysisResult,
    Component,
    CustomUser,
    PasswordResetCode,
)
from .forms import (
    ProjectForm,
    FirmwareForm,
    CustomUserCreationForm,
    SolarPreferenceForm,
    BatteryPreferenceForm,
)
from .services.pipeline import run_analysis
from .services.report_builder import build_report_context
from .services.datasheet_extractor import (
    extract_component_values_from_datasheet,
    extract_mode_values_from_datasheet,
)

logger = logging.getLogger(__name__)


# ============================================================
# HOME
# ============================================================

def home(request):
    if request.user.is_authenticated:
        projects = (
            Project.objects
            .filter(user=request.user)
            .order_by('-created_at')[:10]
        )
    else:
        projects = Project.objects.none()

    return render(
        request,
        'analyzer/home.html',
        {'projects': projects},
    )


# ============================================================
# HELPERS NUMÉRIQUES / GÉNÉRIQUES
# ============================================================

def _to_float(value):
    try:
        if value is None:
            return None

        value = str(value).strip()

        if value == '':
            return None

        return float(value.replace(',', '.'))

    except (ValueError, TypeError):
        return None


def _to_int(value):
    try:
        if value is None:
            return None

        value = str(value).strip()

        if value == '':
            return None

        return int(value)

    except (ValueError, TypeError):
        return None


def _get_indices(post_data, prefix):
    pattern = re.compile(rf'^{re.escape(prefix)}(\d+)$')

    indices = []

    for key in post_data.keys():
        match = pattern.match(key)

        if match:
            indices.append(int(match.group(1)))

    return sorted(set(indices))


# ============================================================
# CRÉATION DE PROJET
# ============================================================

@login_required
def project_create(request):

    if request.method == 'POST':

        draft = None

        draft_id = request.POST.get('draft_id')

        if draft_id:
            draft = get_object_or_404(
                Project,
                id=draft_id,
                user=request.user,
            )

        existing_firmware = draft.firmwares.first() if draft else None

        existing_solar = (
            getattr(draft, 'solar_pref', None) if draft else None
        )

        existing_battery = (
            getattr(draft, 'battery_pref', None) if draft else None
        )

        form = ProjectForm(request.POST, instance=draft)

        firm_form = FirmwareForm(
            request.POST,
            request.FILES,
            instance=existing_firmware,
        )

        solar_form = SolarPreferenceForm(
            request.POST,
            instance=existing_solar,
        )

        battery_form = BatteryPreferenceForm(
            request.POST,
            instance=existing_battery,
        )

        use_saved = request.POST.get('use_saved_components') == '1'

        if use_saved:
            forms_are_valid = bool(
                draft
                and existing_firmware
                and existing_solar
                and existing_battery
            )

        else:
            forms_are_valid = (
                form.is_valid()
                and firm_form.is_valid()
                and solar_form.is_valid()
                and battery_form.is_valid()
            )

        if forms_are_valid:

            if use_saved:
                project = draft

            else:
                project = form.save(commit=False)
                project.user = request.user
                project.save()

            warnings = []

            if not use_saved:

                for category in ('mcu', 'sensor', 'actuator'):
                    _save_components(
                        request.POST,
                        request.FILES,
                        project,
                        category,
                        warnings,
                    )

            if use_saved:
                firmware = existing_firmware

            else:
                firmware = firm_form.save(commit=False)
                firmware.project = project
                firmware.save()

            if not use_saved:

                solar_pref = solar_form.save(commit=False)
                solar_pref.project = project
                solar_pref.save()

                battery_pref = battery_form.save(commit=False)
                battery_pref.project = project
                battery_pref.save()

            total_modes = sum(
                component.modes.count()
                for component in project.components.all()
            )

            has_code = (
                bool((firmware.source_code or '').strip())
                or bool(firmware.code_file)
            )

            if total_modes == 0 and not has_code:

                project.status = 'failed'
                project.save(update_fields=['status'])

                messages.error(
                    request,
                    '❌ Fournissez un code source ou saisissez au moins '
                    'un mode de fonctionnement.'
                )

                if draft:
                    return redirect('wizard_review', project_id=project.id)

                return redirect('project_create')

            # ======================================================
            # ANALYSE : calcul Python + RAG + agents IA (Ollama)
            # ======================================================

            try:
                report_data = run_analysis(project)

            except Exception:
                logger.exception('Échec de l’analyse du projet %s.', project.id)

                project.status = 'failed'
                project.save(update_fields=['status'])

                messages.error(
                    request,
                    'L’analyse n’a pas pu aboutir. Vérifiez les courants, '
                    'les durées et le nombre d’exécutions des modes.'
                )

                return redirect('wizard_review', project_id=project.id)

            profile = report_data['energy'] or {}
            sizing = report_data['sizing']

            result, _created = AnalysisResult.objects.get_or_create(
                project=project
            )

            result.full_report = report_data
            result.summary = sizing['decision']
            result.energy_profile = profile
            result.battery_recommendation = sizing['battery'] or {}
            result.solar_report = sizing['solar'] or {}
            result.adapter_recommendation = sizing['electronics'] or {}
            result.consumption_curve = profile.get('plateaus', [])
            result.daily_energy_mah = profile.get('energy_daily_mah') or 0
            result.daily_energy_mwh = profile.get('energy_daily_mwh') or 0
            result.status = 'completed'
            result.analysis_source = (
                'ollama'
                if report_data['ai'].get('consumption') else 'python'
            )
            result.save()

            project.status = 'completed'
            project.save(update_fields=['status'])

            for warning in warnings:
                messages.warning(request, warning)

            if report_data['ai'].get('error'):
                messages.warning(request, report_data['ai']['error'])

            return redirect('report', project_id=project.id)

        if use_saved and draft:

            messages.error(
                request,
                'Le brouillon est incomplet. Vérifiez les étapes Programme, '
                'Batterie et Solaire avant de relancer l’analyse.'
            )

            return redirect('wizard_review', project_id=draft.id)

    else:

        form = ProjectForm()
        firm_form = FirmwareForm()
        solar_form = SolarPreferenceForm()
        battery_form = BatteryPreferenceForm()

    return render(
        request,
        'analyzer/project_form.html',
        {
            'form': form,
            'firm_form': firm_form,
            'solar_form': solar_form,
            'battery_form': battery_form,
        },
    )


# ============================================================
# WIZARD
# ============================================================

WIZARD_STEPS = (
    ('project', 'Projet et position'),
    ('mcu', 'Microcontrôleur'),
    ('peripherals', 'Capteurs et communications'),
    ('actuators', 'Actionneurs et charges'),
    ('firmware', 'Programme embarqué'),
    ('energy', 'Batterie et solaire'),
    ('review', 'Vérification'),
)


def _wizard_context(step, project=None, **extra):
    current = next(
        index
        for index, item in enumerate(WIZARD_STEPS, start=1)
        if item[0] == step
    )

    steps = []

    for index, (code, label) in enumerate(WIZARD_STEPS, start=1):
        url = None

        # Une étape est accessible dès que le brouillon existe.
        if project is not None and project.pk:
            url_name = 'wizard_project_edit' if code == 'project' else f'wizard_{code}'
            url = reverse(url_name, args=[project.id])

        steps.append({
            'number': index,
            'code': code,
            'label': label,
            'url': url,
            'state': (
                'done' if index < current
                else 'current' if index == current
                else 'todo'
            ),
        })

    context = {
        'wizard_steps': steps,
        'wizard_step': step,
        'wizard_current': current,
        'wizard_total': len(WIZARD_STEPS),
        'wizard_percent': round(current / len(WIZARD_STEPS) * 100),
        'project': project,
    }

    context.update(extra)

    return context


def _wizard_project(request, project_id):
    return get_object_or_404(
        Project,
        id=project_id,
        user=request.user,
    )


def workflow_intro(request):
    return render(request, 'analyzer/workflow_intro.html')


@login_required
def wizard_project(request, project_id=None):
    project = _wizard_project(request, project_id) if project_id else None

    form = ProjectForm(request.POST or None, instance=project)

    if request.method == 'POST' and form.is_valid():

        project = form.save(commit=False)
        project.user = request.user
        project.status = 'draft'
        project.save()

        return redirect('wizard_mcu', project_id=project.id)

    return render(
        request,
        'analyzer/wizard/project.html',
        _wizard_context('project', project, form=form),
    )


def _component_rows(project, categories):
    return (
        project.components
        .filter(category__in=categories)
        .prefetch_related('modes')
        .order_by('id')
    )


def _save_wizard_categories(request, project, categories):
    warnings = []

    with transaction.atomic():

        project.components.filter(category__in=categories).delete()

        for category in categories:
            _save_components(
                request.POST,
                request.FILES,
                project,
                category,
                warnings,
            )

    for warning in warnings:
        messages.warning(request, warning)


@login_required
def wizard_mcu(request, project_id):
    project = _wizard_project(request, project_id)

    if request.method == 'POST':

        if not any(
            value.strip()
            for key, value in request.POST.items()
            if key.startswith('mcu_name_')
        ):
            messages.error(request, 'Ajoutez au moins un microcontrôleur.')

        else:
            _save_wizard_categories(request, project, ('mcu',))

            return redirect('wizard_peripherals', project_id=project.id)

    return render(
        request,
        'analyzer/wizard/components.html',
        _wizard_context(
            'mcu',
            project,
            sections=(('mcu', 'Microcontrôleurs', True),),
            component_rows=_component_rows(project, ('mcu',)),
            previous_url='wizard_project_edit',
            next_label='Continuer vers les capteurs',
        ),
    )


@login_required
def wizard_peripherals(request, project_id):
    project = _wizard_project(request, project_id)

    if request.method == 'POST':

        _save_wizard_categories(
            request,
            project,
            ('sensor', 'communication'),
        )

        return redirect('wizard_actuators', project_id=project.id)

    return render(
        request,
        'analyzer/wizard/components.html',
        _wizard_context(
            'peripherals',
            project,
            sections=(
                ('sensor', 'Capteurs', False),
                ('communication', 'Communications', False),
            ),
            component_rows=_component_rows(
                project,
                ('sensor', 'communication'),
            ),
            previous_url='wizard_mcu',
            next_label='Continuer vers les actionneurs',
        ),
    )


@login_required
def wizard_actuators(request, project_id):
    project = _wizard_project(request, project_id)

    if request.method == 'POST':

        _save_wizard_categories(request, project, ('actuator',))

        return redirect('wizard_firmware', project_id=project.id)

    return render(
        request,
        'analyzer/wizard/components.html',
        _wizard_context(
            'actuators',
            project,
            sections=(('actuator', 'Actionneurs et charges', False),),
            component_rows=_component_rows(project, ('actuator',)),
            previous_url='wizard_peripherals',
            next_label='Continuer vers le programme',
        ),
    )


@login_required
def wizard_firmware(request, project_id):
    project = _wizard_project(request, project_id)

    firmware = project.firmwares.first()

    form = FirmwareForm(
        request.POST or None,
        request.FILES or None,
        instance=firmware,
    )

    if request.method == 'POST' and form.is_valid():

        firmware = form.save(commit=False)
        firmware.project = project
        firmware.save()

        return redirect('wizard_energy', project_id=project.id)

    return render(
        request,
        'analyzer/wizard/firmware.html',
        _wizard_context('firmware', project, form=form),
    )


# ============================================================
# VALIDATION DATASHEET BATTERIE
# ============================================================

def _validate_battery_datasheet(uploaded_file):
    """
    Vérifie la datasheet de batterie :
    - extension PDF ;
    - taille maximale ;
    - signature PDF.
    """

    if not uploaded_file:
        return

    filename = str(uploaded_file.name or '').strip().lower()

    if not filename.endswith('.pdf'):
        raise ValidationError(
            'La datasheet de la batterie doit être fournie au format PDF.'
        )

    maximum_size = 10 * 1024 * 1024

    if uploaded_file.size > maximum_size:
        raise ValidationError(
            'La datasheet de la batterie ne doit pas dépasser 10 Mo.'
        )

    try:
        signature = uploaded_file.read(5)
        uploaded_file.seek(0)

    except (OSError, AttributeError) as exc:
        raise ValidationError(
            'Impossible de lire la datasheet de la batterie.'
        ) from exc

    if signature != b'%PDF-':
        raise ValidationError(
            'Le fichier fourni ne contient pas un document PDF valide.'
        )


@login_required
def wizard_energy(request, project_id):
    project = _wizard_project(request, project_id)

    solar = getattr(project, 'solar_pref', None)
    battery = getattr(project, 'battery_pref', None)

    solar_form = SolarPreferenceForm(
        request.POST or None,
        instance=solar,
        prefix='solar',
    )

    battery_form = BatteryPreferenceForm(
        request.POST or None,
        instance=battery,
        prefix='battery',
    )

    if request.method == 'POST':

        solar_is_valid = solar_form.is_valid()
        battery_is_valid = battery_form.is_valid()

        if solar_is_valid and battery_is_valid:

            battery_datasheet = request.FILES.get('battery_datasheet')

            try:
                _validate_battery_datasheet(battery_datasheet)

            except ValidationError as exc:

                for error_message in exc.messages:
                    messages.error(request, error_message)

            else:

                with transaction.atomic():

                    solar = solar_form.save(commit=False)
                    solar.project = project
                    solar.save()

                    battery = battery_form.save(commit=False)
                    battery.project = project

                    if battery_datasheet:
                        battery.battery_datasheet = battery_datasheet

                    battery.save()

                messages.success(
                    request,
                    'Les préférences énergétiques ont été enregistrées.'
                )

                return redirect('wizard_review', project_id=project.id)

    return render(
        request,
        'analyzer/wizard/energy.html',
        _wizard_context(
            'energy',
            project,
            solar_form=solar_form,
            battery_form=battery_form,
            battery=battery,
        ),
    )


@login_required
def wizard_review(request, project_id):
    project = _wizard_project(request, project_id)

    components = project.components.prefetch_related('modes').all()

    firmware = project.firmwares.first()

    if not firmware:

        messages.error(request, 'Complétez d’abord l’étape Programme.')

        return redirect('wizard_firmware', project_id=project.id)

    if (
        not hasattr(project, 'solar_pref')
        or not hasattr(project, 'battery_pref')
    ):

        messages.error(request, 'Complétez les préférences énergétiques.')

        return redirect('wizard_energy', project_id=project.id)

    return render(
        request,
        'analyzer/wizard/review.html',
        _wizard_context(
            'review',
            project,
            components=components,
            firmware=firmware,
            solar=project.solar_pref,
            battery=project.battery_pref,
        ),
    )


# ============================================================
# SAUVEGARDE DES COMPOSANTS
# ============================================================

def _save_components(post_data, files, project, category, warnings):
    indices = _get_indices(post_data, f'{category}_name_')

    for i in indices:

        name = post_data.get(f'{category}_name_{i}', '').strip()

        if not name:
            continue

        raw_voltage = post_data.get(f'{category}_voltage_{i}', '').strip()

        voltage = _to_float(raw_voltage)

        voltage_provided = bool(raw_voltage)

        try:

            comp = Component(
                project=project,
                name=name,
                category=category,
            )

            if voltage is not None:
                comp.voltage = voltage

            if category == 'mcu':

                nominal = _to_float(
                    post_data.get(f'{category}_nominal_{i}', '')
                )
                cores = _to_int(post_data.get(f'{category}_cores_{i}', ''))
                fmin = _to_float(post_data.get(f'{category}_fmin_{i}', ''))
                fmax = _to_float(post_data.get(f'{category}_fmax_{i}', ''))
                fused = _to_float(post_data.get(f'{category}_fused_{i}', ''))

                if nominal is not None:
                    comp.nominal_current_ma = nominal

                if cores is not None:
                    comp.cores_count = cores

                if fmin is not None:
                    comp.freq_min_mhz = fmin

                if fmax is not None:
                    comp.freq_max_mhz = fmax

                if fused is not None:
                    comp.freq_used_mhz = fused

            if category == 'actuator':

                sep_power = post_data.get(f'{category}_seppower_{i}', '')

                comp.separate_power = sep_power == 'on'

            datasheet_key = f'{category}_datasheet_{i}'

            if datasheet_key in files:
                comp.datasheet = files[datasheet_key]

            comp.save()

            _apply_datasheet_fallback(
                comp,
                warnings,
                override_voltage=not voltage_provided,
            )

            _save_modes_for_component(
                post_data,
                comp,
                category,
                i,
                warnings,
            )

        except (ValueError, TypeError) as error:

            warnings.append(
                f'⚠️ {category.upper()} #{i + 1} : erreur ({error})'
            )


def _apply_datasheet_fallback(component, warnings, override_voltage=False):
    extracted = extract_component_values_from_datasheet(component)

    updated = False

    if override_voltage:

        voltage = _to_float(extracted.get('voltage'))

        if voltage is not None:
            component.voltage = voltage
            updated = True

    if component.category == 'mcu':

        for field, key in [
            ('nominal_current_ma', 'nominal_current_ma'),
            ('cores_count', 'cores_count'),
            ('freq_min_mhz', 'freq_min_mhz'),
            ('freq_max_mhz', 'freq_max_mhz'),
            ('freq_used_mhz', 'freq_used_mhz'),
        ]:

            if getattr(component, field) is None:

                value = (
                    _to_float(extracted.get(key))
                    if 'count' not in field
                    else _to_int(extracted.get(key))
                )

                if value is not None:
                    setattr(component, field, value)
                    updated = True

    if updated:
        component.save()


def _save_modes_for_component(
    post_data,
    component,
    category,
    comp_idx,
    warnings,
):
    mode_prefix = f'{category}_mode_name_{comp_idx}_'

    indices = _get_indices(post_data, mode_prefix)

    for mi in indices:

        name = post_data.get(
            f'{category}_mode_name_{comp_idx}_{mi}',
            '',
        ).strip()

        if not name:
            continue

        current = _to_float(
            post_data.get(f'{category}_mode_current_{comp_idx}_{mi}', '')
        )

        duration = _to_float(
            post_data.get(f'{category}_mode_duration_{comp_idx}_{mi}', '')
        )

        count = _to_int(
            post_data.get(f'{category}_mode_count_{comp_idx}_{mi}', '1')
        )

        if count is None or count <= 0:
            count = 1

        if (current is None or duration is None) and component.datasheet:

            extracted = extract_mode_values_from_datasheet(component, name)

            if current is None:
                current = _to_float(extracted.get('current_ma'))

            if duration is None:
                duration = _to_float(extracted.get('duration_ms'))

        if current is None or duration is None:

            warnings.append(
                f'⚠️ {component.name}/{name} : valeurs manquantes.'
            )

            continue

        try:

            ConsumptionMode.objects.create(
                component=component,
                name=name,
                current_ma=current,
                duration_ms=duration,
                count_per_cycle=count,
            )

        except (ValueError, TypeError) as error:

            warnings.append(
                f'⚠️ {component.name}/{name} : erreur ({error})'
            )


# ============================================================
# RAPPORT
# ============================================================

@login_required
def report(request, project_id):
    """Tableau de bord des valeurs prédites d'un projet analysé."""

    project = get_object_or_404(
        Project,
        id=project_id,
        user=request.user,
    )

    try:
        result = project.result
    except AnalysisResult.DoesNotExist:
        result = None

    return render(
        request,
        'analyzer/report.html',
        build_report_context(project, result),
    )


@login_required
def report_measurement(request, project_id):
    """Enregistre (ou efface) le courant moyen mesuré sur le dispositif."""

    project = get_object_or_404(Project, id=project_id, user=request.user)
    target = reverse('report', args=[project.id]) + '#mesure'

    if request.method != 'POST':
        return redirect(target)

    if request.POST.get('action') == 'clear':
        project.measured_current_ma = None
        project.measurement_notes = ''
        project.measured_at = None
        project.save(update_fields=['measured_current_ma', 'measurement_notes', 'measured_at'])
        messages.info(request, 'La mesure a été retirée.')
        return redirect(target)

    value = _to_float(request.POST.get('value'))

    if value is None or value <= 0:
        messages.error(request, 'Indiquez un courant moyen mesuré supérieur à zéro.')
        return redirect(target)

    if request.POST.get('unit') == 'ua':
        value /= 1000

    project.measured_current_ma = value
    project.measurement_notes = request.POST.get('notes', '').strip()[:200]
    project.measured_at = timezone.now()
    project.save(update_fields=['measured_current_ma', 'measurement_notes', 'measured_at'])

    messages.success(request, 'Mesure enregistrée : la comparaison est à jour.')
    return redirect(target)


# ============================================================
# LISTE DES PROJETS
# ============================================================

@login_required
def project_list(request):

    projects = (
        Project.objects
        .filter(user=request.user)
        .select_related('result')
        .order_by('-created_at')
    )

    return render(
        request,
        'analyzer/project_list.html',
        {'projects': projects},
    )


# ============================================================
# INSCRIPTION
# ============================================================

def register(request):

    if request.method == 'POST':

        form = CustomUserCreationForm(request.POST)

        if form.is_valid():

            user = form.save()

            login(request, user)

            messages.success(request, f"Bienvenue {user.display_name}")

            return redirect('home')

    else:

        form = CustomUserCreationForm()

    return render(
        request,
        'analyzer/auth/register.html',
        {'form': form},
    )


# ============================================================
# PROFIL
# ============================================================

@login_required
def profile(request):

    return render(request, 'analyzer/auth/profile.html')


# ============================================================
# MOT DE PASSE OUBLIÉ
# ============================================================

def forgot_password(request):
    """
    Étape 1 :
    l'utilisateur saisit son adresse e-mail et reçoit
    un code à 6 chiffres.
    """

    if request.method == 'POST':

        email = request.POST.get('email', '').strip().lower()

        if not email:

            messages.error(request, "Veuillez saisir votre adresse e-mail.")

            return render(
                request,
                'analyzer/auth/forgot_password.html',
                {'email': email},
            )

        user = CustomUser.objects.filter(email__iexact=email).first()

        if user is None:

            messages.error(
                request,
                "Aucun compte n'est associé à cette adresse e-mail."
            )

            return render(
                request,
                'analyzer/auth/forgot_password.html',
                {'email': email},
            )

        reset_code = PasswordResetCode.create_for_user(user)

        subject = "Code de réinitialisation de votre mot de passe"

        message = (
            f"Bonjour {user.display_name},\n\n"
            f"Votre code de réinitialisation est : {reset_code.code}\n\n"
            "Ce code est valable pendant 10 minutes.\n"
            "Ne communiquez jamais ce code à une autre personne.\n\n"
            "Assistant Énergétique IoT"
        )

        try:

            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )

        except Exception:

            reset_code.is_used = True
            reset_code.save(update_fields=['is_used'])

            logger.exception("Erreur d'envoi de l'e-mail.")

            messages.error(
                request,
                "Impossible d'envoyer le code. Vérifiez la configuration "
                "du serveur d'e-mail."
            )

            return render(
                request,
                'analyzer/auth/forgot_password.html',
                {'email': email},
            )

        request.session['password_reset_user_id'] = user.pk
        request.session['password_reset_verified'] = False

        messages.success(
            request,
            "Un code à 6 chiffres a été envoyé à votre adresse e-mail."
        )

        return redirect('verify_reset_code')

    return render(request, 'analyzer/auth/forgot_password.html')


# ============================================================
# VÉRIFICATION DU CODE
# ============================================================

def verify_reset_code(request):
    """
    Étape 2 :
    l'utilisateur saisit le code reçu par e-mail.
    """

    user_id = request.session.get('password_reset_user_id')

    if not user_id:

        messages.error(request, "Commencez par saisir votre adresse e-mail.")

        return redirect('forgot_password')

    user = CustomUser.objects.filter(pk=user_id).first()

    if user is None:

        request.session.pop('password_reset_user_id', None)
        request.session.pop('password_reset_verified', None)

        messages.error(request, "Utilisateur introuvable.")

        return redirect('forgot_password')

    if request.method == 'POST':

        submitted_code = request.POST.get('code', '').strip()

        if not submitted_code.isdigit() or len(submitted_code) != 6:

            messages.error(
                request,
                "Le code doit contenir exactement 6 chiffres."
            )

            return render(
                request,
                'analyzer/auth/verify_reset_code.html',
                {'email': user.email},
            )

        # Toujours le code le plus récent non utilisé.
        reset_code = (
            PasswordResetCode.objects
            .filter(user=user, is_used=False)
            .order_by('-id')
            .first()
        )

        if reset_code is None:

            messages.error(
                request,
                "Aucun code valide n'a été trouvé. Demandez un nouveau code."
            )

            return redirect('forgot_password')

        if not reset_code.is_valid():

            reset_code.is_used = True
            reset_code.save(update_fields=['is_used'])

            messages.error(
                request,
                "Ce code a expiré ou le nombre maximal d'essais a été atteint."
            )

            return redirect('forgot_password')

        if not constant_time_compare(reset_code.code, submitted_code):

            reset_code.attempts += 1
            reset_code.save(update_fields=['attempts'])

            remaining_attempts = max(0, 5 - reset_code.attempts)

            if remaining_attempts == 0:

                reset_code.is_used = True
                reset_code.save(update_fields=['is_used'])

                messages.error(
                    request,
                    "Trop de tentatives incorrectes. Demandez un nouveau code."
                )

                return redirect('forgot_password')

            messages.error(
                request,
                f"Code incorrect. Il vous reste {remaining_attempts} "
                "tentative(s)."
            )

            return render(
                request,
                'analyzer/auth/verify_reset_code.html',
                {'email': user.email},
            )

        reset_code.is_used = True
        reset_code.save(update_fields=['is_used'])

        request.session['password_reset_verified'] = True

        messages.success(
            request,
            "Code vérifié. Vous pouvez maintenant choisir un nouveau "
            "mot de passe."
        )

        return redirect('new_password')

    return render(
        request,
        'analyzer/auth/verify_reset_code.html',
        {'email': user.email},
    )


# ============================================================
# NOUVEAU MOT DE PASSE
# ============================================================

def new_password(request):
    """
    Étape 3 :
    l'utilisateur choisit son nouveau mot de passe.
    """

    user_id = request.session.get('password_reset_user_id')

    is_verified = request.session.get('password_reset_verified', False)

    if not user_id or not is_verified:

        messages.error(
            request,
            "Vous devez d'abord vérifier le code reçu par e-mail."
        )

        return redirect('forgot_password')

    user = CustomUser.objects.filter(pk=user_id).first()

    if user is None:

        request.session.pop('password_reset_user_id', None)
        request.session.pop('password_reset_verified', None)

        messages.error(request, "Utilisateur introuvable.")

        return redirect('forgot_password')

    if request.method == 'POST':

        password1 = request.POST.get('password1', '')
        password2 = request.POST.get('password2', '')

        if not password1 or not password2:

            messages.error(
                request,
                "Veuillez remplir les deux champs de mot de passe."
            )

            return render(request, 'analyzer/auth/new_password.html')

        if password1 != password2:

            messages.error(
                request,
                "Les deux mots de passe ne correspondent pas."
            )

            return render(request, 'analyzer/auth/new_password.html')

        try:

            validate_password(password1, user=user)

        except ValidationError as errors:

            for error in errors.messages:
                messages.error(request, error)

            return render(request, 'analyzer/auth/new_password.html')

        user.set_password(password1)
        user.save(update_fields=['password'])

        PasswordResetCode.objects.filter(
            user=user,
            is_used=False,
        ).update(is_used=True)

        request.session.pop('password_reset_user_id', None)
        request.session.pop('password_reset_verified', None)

        messages.success(
            request,
            "Votre mot de passe a été modifié. Vous pouvez maintenant "
            "vous connecter."
        )

        return redirect('login')

    return render(request, 'analyzer/auth/new_password.html')