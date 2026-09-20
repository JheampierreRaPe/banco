import 'package:flutter/material.dart';

import '../app_version.dart';

/// Etiqueta discreta con la version visible del build (F-T30).
///
/// Montada por las tres pantallas relevantes (`/entry`, `/login` y `/kyc`)
/// usando el mismo widget y `Key('app-version')`, para confirmar que build esta
/// instalado. El texto proviene de la fuente unica [kAppVersion]; ninguna
/// pantalla define la version.
///
/// Es puro render local: no consulta red, no persiste ni decide nada. Usa los
/// tokens del diseno "Eucalipto y Ocre" (`label-sm` con color de texto
/// secundario) y es no intrusiva para no romper los estados de cada pantalla.
class AppVersionLabel extends StatelessWidget {
  const AppVersionLabel({super.key});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Text(
      'Version $kAppVersion',
      key: const Key('app-version'),
      style: theme.textTheme.labelSmall?.copyWith(
        color: theme.colorScheme.onSurfaceVariant,
      ),
    );
  }
}
