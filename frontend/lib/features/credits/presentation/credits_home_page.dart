import 'package:flutter/material.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../data/credits_service.dart';
import '../data/my_credits_service.dart';
import 'credits_simulator_page.dart';
import 'my_credits_section.dart';

/// Home de créditos `/credits` (E3-T23; spec
/// `docs/design/creditos-pantallas.md` §7, mockup `creditos-mis-creditos.png`).
///
/// Dos secciones con `SegmentedButton` arriba (`Simulador` | `Mis créditos`,
/// decisión del dueño): la pestaña Simulador monta el simulador actual
/// (embebido, sin su propio `AppBar`); la pestaña Mis créditos monta el
/// listado del titular. El TopBar es `Créditos`.
///
/// [simulatorService] y [myCreditsService] son inyectables para tests; la
/// ruta los resuelve desde `creditsServiceFactory` y
/// `creditApplicationsServiceFactory` (sin tocar `app_router.dart`).
class CreditsHomePage extends StatefulWidget {
  const CreditsHomePage({
    super.key,
    this.simulatorService,
    this.myCreditsService,
  });

  final CreditsServiceBase? simulatorService;
  final MyCreditsServiceBase? myCreditsService;

  @override
  State<CreditsHomePage> createState() => _CreditsHomePageState();
}

enum _CreditsTab { simulator, mine }

class _CreditsHomePageState extends State<CreditsHomePage> {
  _CreditsTab _tab = _CreditsTab.simulator;

  void _goSimulator() => setState(() => _tab = _CreditsTab.simulator);

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Créditos')),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
            child: SegmentedButton<_CreditsTab>(
              key: const Key('credits-segments'),
              segments: const [
                ButtonSegment<_CreditsTab>(
                  value: _CreditsTab.simulator,
                  label: Text('Simulador'),
                  icon: Icon(Icons.calculate_outlined),
                ),
                ButtonSegment<_CreditsTab>(
                  value: _CreditsTab.mine,
                  label: Text('Mis créditos'),
                  icon: Icon(Icons.folder_open_outlined),
                ),
              ],
              selected: {_tab},
              onSelectionChanged: (selected) =>
                  setState(() => _tab = selected.first),
              showSelectedIcon: false,
              style: SegmentedButton.styleFrom(
                backgroundColor: AppColors.surfaceContainerHigh,
                foregroundColor: AppColors.primary,
                selectedBackgroundColor: AppColors.primary,
                selectedForegroundColor: AppColors.onPrimary,
                textStyle: const TextStyle(
                  fontSize: 14,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Expanded(
            child: _tab == _CreditsTab.simulator
                ? CreditsSimulatorPage(
                    service: widget.simulatorService,
                    embedded: true,
                  )
                : MyCreditsSection(
                    service: widget.myCreditsService,
                    onGoSimulator: _goSimulator,
                  ),
          ),
        ],
      ),
    );
  }
}
