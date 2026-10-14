import 'package:flutter/material.dart';
import 'package:qr_flutter/qr_flutter.dart';

import '../../../core/theme/app_colors.dart';

/// Vista del QR de cobro (E4-T09 / HU14, contrato `docs/05` §6.5).
///
/// Renderiza el `qr_content` del backend (sobre canonico `BO-QR1`
/// base64url, `String` escaneable) como un QR real con `qr_flutter`.
/// Presentacion pura: no firma, no interpreta el contenido, no decide
/// nada.
///
/// El `payload` estructurado del backend (objeto) NO se renderiza: no
/// es escaneable.
///
/// > Sin PII: el `qr_content` es opaco; nunca se escribe en logs.
class QrPayloadView extends StatelessWidget {
  const QrPayloadView({super.key, required this.qrContent, this.size = 208});

  /// Sobre canonico `BO-QR1` del backend (contenido escaneable).
  final String qrContent;

  /// Lado del QR en px.
  final double size;

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: size + 32,
          height: size + 32,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: Colors.white,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: AppColors.outlineVariant),
          ),
          child: QrImageView(
            key: const Key('merchant-qr-image'),
            data: qrContent,
            version: QrVersions.auto,
            size: size,
            backgroundColor: Colors.white,
            // Expone el contenido para accesibilidad y permite a los
            // tests verificar el dato renderizado (`QrImageView` guarda
            // `data` en un campo privado sin getter publico).
            semanticsLabel: qrContent,
          ),
        ),
        const SizedBox(height: 8),
        SelectableText(
          qrContent,
          key: const Key('merchant-qr-payload'),
          maxLines: 2,
          style: const TextStyle(
            fontSize: 11,
            color: AppColors.secondaryText,
            fontFamily: 'monospace',
          ),
        ),
      ],
    );
  }
}
