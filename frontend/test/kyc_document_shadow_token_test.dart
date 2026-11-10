import 'dart:io';
import 'dart:typed_data';

import 'package:banca_online/core/theme/app_colors.dart';
import 'package:banca_online/core/theme/app_spacing.dart';
import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:banca_online/features/kyc/presentation/kyc_document_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Regresión F-T38 (dictamen `refactorizacion-ui`): la sombra de la tarjeta
/// de captura del DNI sale del sistema de tokens (`docs/20` §5,
/// [AppShadows.cardList], tinte de `primary-container`), sin hex hardcodeado
/// en la frontera KYC.
class _FakeKycService implements KycService {
  const _FakeKycService();

  @override
  Future<KycChallenge> challenge() async => const KycChallenge(
        token: 'tok-abc',
        steps: ['front', 'blink'],
        expiresIn: 300,
      );

  @override
  Future<KycSubmitResult> submit({
    required String challengeToken,
    required String documentType,
    required String documentNumber,
    required KycApplicant applicant,
    required Map<String, List<Uint8List>> framesByTask,
  }) async =>
      const KycSubmitResult(overallResult: true, detailCode: 'OK');
}

String _readDocumentPageSource() {
  const candidates = [
    'lib/features/kyc/presentation/kyc_document_page.dart',
    'frontend/lib/features/kyc/presentation/kyc_document_page.dart',
  ];
  for (final path in candidates) {
    final file = File(path);
    if (file.existsSync()) return file.readAsStringSync();
  }
  fail('no se encontró kyc_document_page.dart (cwd: ${Directory.current.path})');
}

void main() {
  test('la tarjeta de captura no hardcodea el hex de la sombra', () {
    final source = _readDocumentPageSource();
    expect(
      source.contains('0x0D2B4034'),
      isFalse,
      reason: 'kyc_document_page.dart debe derivar la sombra de tokens, '
          'sin el hex hardcodeado Color(0x0D2B4034)',
    );
    expect(
      source.contains('AppShadows.cardList'),
      isTrue,
      reason: 'la sombra debe salir del token AppShadows.cardList (docs/20 §5)',
    );
  });

  testWidgets('la sombra renderizada es el token shadow.card', (tester) async {
    final c = KycFlowController(service: const _FakeKycService());
    addTearDown(c.dispose);
    await c.loadChallenge();

    await tester.pumpWidget(
      MaterialApp(home: KycDocumentPage(controller: c)),
    );
    await tester.pumpAndSettle();

    final shadows = tester
        .widgetList<Container>(find.byType(Container))
        .map((w) => w.decoration)
        .whereType<BoxDecoration>()
        .expand((d) => d.boxShadow ?? const <BoxShadow>[])
        .toList();
    expect(shadows, isNotEmpty, reason: 'la tarjeta debe tener sombra');

    for (final shadow in shadows) {
      expect(
        shadow.color,
        AppShadows.card.color,
        reason: 'la sombra proviene del token AppShadows.card',
      );
    }
    // El token es el tinte de `primary-container` al 5% (docs/20 §5:
    // `shadow.card = 0 2px 12px rgba(43,64,52,0.05)`; mismos canales RGB que
    // `primary-container` = #2B4034, alfa 0x0D/255).
    final token = AppShadows.card.color;
    expect(token.r, AppColors.primaryContainer.r);
    expect(token.g, AppColors.primaryContainer.g);
    expect(token.b, AppColors.primaryContainer.b);
    expect((token.a * 255).round(), 0x0D);
  });
}
